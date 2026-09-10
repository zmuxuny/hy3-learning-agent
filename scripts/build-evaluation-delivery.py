"""Assemble a deterministic, portable evaluation data package from repository files."""
import hashlib
import json
import shutil
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT/'交付材料'
ART = ROOT/'evaluation/artifacts'
RESULTS = {
    '主体实验': ART/'decisionbench-final-method-20260910/automatic',
    '条件验证': ART/'condition-validation-20260910/automatic',
    '卷积诊断': ART/'condition-insight-20260910/automatic',
    '人工一致性': ART/'human-confirmation-20260910',
}


def files(folder):
    return sorted(p for p in folder.rglob('*') if p.is_file() and '__pycache__' not in p.parts
                  and p.suffix not in ('.pyc', '.gz', '.zip') and 'verification' not in p.relative_to(folder).parts)


def build():
    result_root = OUT/'实验结果'
    if result_root.exists():
        shutil.rmtree(result_root)
    for name, source in RESULTS.items():
        dest = result_root/name
        dest.mkdir(parents=True)
        for p in sorted(source.iterdir()):
            if p.suffix in ('.csv', '.json'):
                shutil.copyfile(p, dest/p.name)
    folders = [ROOT/'evaluation'/p for p in ('datasets', 'src', 'schemas', 'scripts')]
    folders += [ART/name for name in ('decisionbench-final-method-20260910',
                'decisionbench-severe-validation-20260910', 'condition-validation-20260910',
                'condition-insight-20260910', 'human-confirmation-20260910')]
    folders += [ART/'decisionbench-study-20260910'/part for part in ('evidence', 'review', 'automatic')]
    folders += [OUT/'人工标注']
    selected = sorted({p for folder in folders for p in files(folder)} | {ROOT/'evaluation/pyproject.toml'})
    manifest = {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in selected}
    instructions = '''# 评测数据与离线复算

本包包含测试输入、原始决策证据、模型请求与响应、人工原始标注、协商参考、统计结果及复算代码。目录保留仓库相对路径。包内SHA256SUMS.json逐文件列出摘要。

Python 3.11或以上，解压后在本目录运行：

```bash
python -m venv .venv
.venv/bin/pip install ./evaluation
PYTHONPATH=evaluation/src .venv/bin/python evaluation/scripts/summarize_final_method.py --archive evaluation/artifacts/decisionbench-final-method-20260910 --output /tmp/recomputed-main
PYTHONPATH=evaluation/src .venv/bin/python evaluation/scripts/summarize_condition_validation.py --archive evaluation/artifacts/condition-validation-20260910 --output /tmp/recomputed-conditions
PYTHONPATH=evaluation/src .venv/bin/python evaluation/scripts/summarize_condition_insight.py --archive evaluation/artifacts/condition-insight-20260910 --output /tmp/recomputed-insight
python evaluation/scripts/summarize_human_confirmation.py --output /tmp/recomputed-human
```

以上命令不调用模型。输出分别与对应档案的automatic目录及human-confirmation-20260910中的CSV/JSON比较。主体实验200次评分，条件验证36次评分，卷积诊断单独统计；人工比较使用48个应用案例。两份原始标注与协商参考在交付材料/人工标注。

完整产品、图文报告和Demo由仓库根目录的第三阶段交付说明提供。
'''
    with zipfile.ZipFile(OUT/'评测数据.zip', 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        payloads = [('README.md', instructions.encode()), ('SHA256SUMS.json', (json.dumps(manifest, ensure_ascii=False, indent=2)+'\n').encode())]
        payloads += [(str(p.relative_to(ROOT)), p.read_bytes()) for p in selected]
        for name, content in payloads:
            info = zipfile.ZipInfo(name, date_time=(2026, 9, 11, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, content, compresslevel=9)
    exported = sorted(p for folder in (result_root, OUT/'人工标注') for p in folder.rglob('*') if p.is_file())
    exported += [OUT/'评测数据.zip']
    (OUT/'SHA256SUMS.json').write_text(json.dumps({str(p.relative_to(OUT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in exported}, ensure_ascii=False, indent=2)+'\n')
    print(f'Packaged {len(selected)} source files; ZIP {(OUT/"评测数据.zip").stat().st_size} bytes')


if __name__ == '__main__':
    build()
