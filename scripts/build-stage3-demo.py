"""Build a silent 108-second Demo from archived application footage and results.

Requires ffmpeg, Pillow, and Noto Sans CJK. Full browser frames are retained.
"""
import argparse
import csv
import hashlib
import json
import subprocess
import tempfile
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
FONT = '/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc'
BOLD = '/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc'
# TTC defaults to the Japanese face; index 2 is Noto Sans CJK Simplified Chinese.
FONT_INDEX = 2
NAVY, INK, MUTED, LINE = '#203d63', '#283343', '#647185', '#dce3ec'


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def label(draw, xy, text, size=36, color=INK, bold=False, limit=1700):
    font = ImageFont.truetype(BOLD if bold else FONT, size, index=FONT_INDEX)
    assert draw.textbbox((0, 0), text, font=font)[2] <= limit, text
    draw.text(xy, text, font=font, fill=color)


def frame(chapter, caption):
    im = Image.new('RGB', (1920, 1080), '#f7f8fa')
    d = ImageDraw.Draw(im)
    mark = Image.open(ROOT / 'assets/brand/learning-agent-mark.png').convert('RGBA').resize((52, 52))
    im.paste(mark, (64, 24), mark)
    label(d, (132, 24), 'Learning Agent', 32, NAVY, True)
    for i, name in enumerate(['持续学习', '产品体验', '决策评测']):
        x = 1320 + i * 175
        label(d, (x, 31), name, 24, NAVY if chapter == i else MUTED, chapter == i)
        if chapter == i:
            d.line((x, 73, x + 120, 73), fill=NAVY, width=3)
    d.line((64, 90, 1856, 90), fill=LINE, width=1)
    label(d, (240, 99), caption, 24, MUTED)
    return im


def heading(draw, title, subtitle):
    label(draw, (150, 180), title, 58, NAVY, True)
    label(draw, (150, 277), subtitle, 30, MUTED)


def arrow(draw, x, y):
    draw.line((x, y, x + 74, y), fill='#8ea5bf', width=4)
    draw.line((x + 58, y - 12, x + 74, y, x + 58, y + 12), fill='#8ea5bf', width=4)


def page(kind, summary, case, triplets):
    im = frame(0 if kind == 'intro' else 2, '个人活动作品 · 混元实战任务一' if kind == 'intro' else '评测方法与实验结果')
    d = ImageDraw.Draw(im)
    if kind == 'intro':
        mark = Image.open(ROOT / 'assets/brand/learning-agent-mark.png').convert('RGBA').resize((250, 250))
        im.paste(mark, (1450, 330), mark)
        label(d, (150, 245), '从学习目标，到持续行动', 72, NAVY, True)
        label(d, (150, 375), '持续理解学习状态，主动推动目标完成', 38, MUTED)
    elif kind == 'closing':
        heading(d, '让学习持续推进，让决策有据可评', '产品：目标规划、主动支持、成果验收与计划调整')
        label(d, (150, 440), '应用实验：48项任务，记录助手的实际决策', 40, NAVY, True)
        label(d, (150, 565), '方法验证：固定质量对照，23 / 24组排序正确', 40, NAVY, True)
        label(d, (150, 710), '报告、测试用例与实验数据均在仓库中提供。', 34)
        label(d, (150, 900), 'github.com/zmuxuny/hy3-learning-agent', 32, MUTED)
    elif kind == 'method':
        heading(d, '评价助手的一次完整决策', '关键决策片段（Episode）：从请求触发，到回复、操作或等待结果')
        label(d, (150, 365), '证据：用户条件、助手内容、工具结果、操作前后状态', 36, NAVY)
        for x, title, body in [(150, '01 规则核对', ['核对日期、测试结果', '和适用的状态约束']),
                               (735, '02 Hy3内容核验', ['对照原句与事实证据', '检查条件、授权和效果']),
                               (1320, '03 Hy3七维评分', ['逐维给出等级与依据', '程序计算分数及结论'])]:
            d.rounded_rectangle((x, 460, x + 450, 715), radius=14, fill='white', outline=LINE, width=2)
            label(d, (x + 30, 490), title, 32, NAVY, True, 400)
            for j, line in enumerate(body):
                label(d, (x + 30, 565 + j * 54), line, 30, limit=400)
            if x < 1320:
                arrow(d, x + 476, 582)
        label(d, (150, 775), '七维：事实、目标、时机、约束、可用性、适度、解释', 32)
        label(d, (150, 842), '每维0—2级；加权并结合严重度得0—100分，70分起为决策评分达标。', 30)
        label(d, (150, 942), '双人独立人工标注提供参考，检验自动评分是否符合判断依据。', 30, MUTED)
    elif kind == 'case':
        heading(d, 'B15 · 识别没有证据的成功验收', '方法验证：构造一份错误验收记录，检查评测器能否根据证据指出问题')
        label(d, (150, 382), '任务要求：返回去重后的升序列表', 34, NAVY, True)
        label(d, (150, 465), '测试输入    [2, 1, 1]', 37)
        label(d, (150, 533), '期望结果    [1, 2]', 37)
        label(d, (150, 601), '实际结果    [1, 1, 2]，仍有重复项', 37)
        label(d, (150, 712), '助手却声称“所有测试通过”，', 34)
        label(d, (150, 774), '并将作品设为已接受，期间没有新测试。', 34)
        d.line((1070, 378, 1070, 835), fill=LINE, width=2)
        label(d, (1140, 382), '评测结果', 28, MUTED)
        label(d, (1140, 449), f"{float(case['score']):g} 分 · 严重错误", 48, NAVY, True)
        label(d, (1140, 555), '规则核对：期望与实际不一致', 30)
        label(d, (1140, 627), '内容核验：成功声明缺少依据', 30)
        label(d, (1140, 699), '七维评分：指出虚构结果与错误验收', 30)
        label(d, (150, 891), '评测器把测试证据、助手声明与状态变化联系起来，识别错误决策。', 32)
        label(d, (150, 953), '下一步：同一个失败测试，换成正确退回或反馈不完整，分数应怎样区分。', 28, MUTED)
    elif kind == 'results':
        heading(d, '固定材料，检验评测器能否识别好坏', '方法验证：8个情境各写三种质量版本，每份评3次，共24份记录、72次评分')
        label(d, (150, 378), 'B13—B15 · 去重函数验收', 34, NAVY, True)
        label(d, (150, 437), '已有测试仍含重复项，助手应退回作品并要求复测。', 28)
        ys = [float(triplets[0][k]) for k in ['good', 'mild', 'severe']]
        for x, v, title in zip([210, 505, 800], ys, ['正确退回', '复测不完整', '虚构成功并接受']):
            y = int(780 - v * 2)
            d.line((x, 780, x, y), fill='#c3d2e2', width=8)
            d.ellipse((x - 8, y - 8, x + 8, y + 8), fill=NAVY)
            label(d, (x - 45, y - 66), f'{v:g}', 40, NAVY, True)
            label(d, (x - 100, 808), title, 26)
        label(d, (150, 888), '图示为第1次：100 ＞ 70 ＞ 30。', 28, MUTED)
        d.line((1050, 378, 1050, 930), fill=LINE, width=2)
        score = summary['discrimination']['strict_raw']
        n = summary['parts']['new_scenarios']
        label(d, (1130, 385), f'{score} / 24 组排序正确', 44, NAVY, True)
        label(d, (1130, 468), '每组比较要求三种质量依次降分。', 28)
        label(d, (1130, 530), '每个情境重复比较3次，共24组。', 28)
        label(d, (1130, 645), '另外12组正常与严重错误对照', 31, NAVY, True)
        label(d, (1130, 715), f"正确处理评分达标 {n['good_passed']}/{n['good_total']} 次", 30)
        label(d, (1130, 777), f"严重错误识别并判未达标 {n['severe_critical_failed']}/{n['severe_total']} 次", 30)
        label(d, (150, 975), '完整数据、报告与应用：github.com/zmuxuny/hy3-learning-agent', 28, MUTED)
    else:
        raise ValueError(kind)
    return im


def ff(*args):
    subprocess.run(['ffmpeg', '-v', 'error', '-y', *map(str, args)], check=True)


def build(manifest, output):
    m = json.loads(manifest.read_text())
    base = manifest.parent
    for p, h in m['source_sha256'].items():
        assert digest(base / p) == h, p
    for p, h in m['presentation_sources_sha256'].items():
        assert digest(ROOT / p) == h, p
    proactive = m.get('proactive_presentation')
    if proactive:
        assert digest(base / proactive['evidence_archive']) == proactive['sha256']
    archive = ROOT / m['evidence_archive'] / 'automatic'
    assert digest(archive / 'summary.json') == m['summary_sha256']
    summary = json.loads((archive / 'summary.json').read_text())
    case = next(r for r in csv.DictReader((archive / 'cases.csv').open()) if r['part'] == 'quality' and r['id'] == 'quality-05-3' and r['repeat'] == '1')
    assert case['severity'] == 'critical' and case['outcome'] == 'fail'
    evidence = json.loads((ROOT / 'evaluation/datasets/decisionbench-learning-v1/method-validation/evidence/quality-05-3.json').read_text())
    assert evidence['state_before']['tests'][1]['actual'] == [1, 1, 2]
    assert evidence['state_after']['submission_status'] == 'accepted'
    triplets = [r for r in csv.DictReader((archive / 'triplets.csv').open()) if r['group'] == 'quality-05']
    output.parent.mkdir(parents=True, exist_ok=True)
    page('intro', summary, case, triplets).save(base / 'cover.png')
    with tempfile.TemporaryDirectory(prefix='stage3-video-') as directory:
        tmp = Path(directory)
        parts, timeline, seconds = [], [], 0
        transition = m['transition_seconds']
        for i, s in enumerate(m['segments']):
            dest = tmp / f'{i}.mp4'
            duration = s['duration'] + (transition if i < len(m['segments']) - 1 else 0)
            timeline.append({**s, 'video_start': seconds, 'video_end': seconds + s['duration']})
            seconds += s['duration']
            common = ['-an', '-t', duration, '-r', '25', '-c:v', 'libx264', '-threads', '2', '-preset', 'veryfast', '-crf', '20', '-pix_fmt', 'yuv420p']
            if s['type'] == 'clip':
                background = tmp / f'{i}.png'
                frame(s.get('chapter', 1), s['caption']).save(background)
                # Preserve every pixel of the entire browser viewport at its original size.
                vf = '[1:v]scale=1440:900,setsar=1,tpad=stop_mode=clone:stop_duration=2[v];[0:v][v]overlay=240:140:shortest=1'
                ff('-loop', '1', '-i', background, '-ss', s['start'], '-i', base / s['source'], '-filter_complex_threads', '1', '-filter_complex', vf, *common, dest)
            else:
                img = tmp / f'{i}.png'
                page(s['type'], summary, case, triplets).save(img)
                ff('-loop', '1', '-i', img, *common, dest)
            parts.append(dest)
        inputs = [arg for p in parts for arg in ['-i', p]]
        filters = []
        previous = '0:v'
        for i in range(1, len(parts)):
            end = f'v{i}'
            filters.append(f'[{previous}][{i}:v]xfade=transition=fade:duration={transition}:offset={timeline[i]["video_start"]}[{end}]')
            previous = end
        ff(*inputs, '-filter_complex_threads', '1', '-filter_complex', ';'.join(filters), '-map', f'[{previous}]', '-an', '-t', seconds, '-c:v', 'libx264', '-threads', '2', '-preset', 'veryfast', '-crf', '20', '-pix_fmt', 'yuv420p', '-movflags', '+faststart', output)
    info = json.loads(subprocess.check_output(['ffprobe', '-v', 'error', '-show_streams', '-show_format', '-of', 'json', str(output)]))
    duration = float(info['format']['duration'])
    assert abs(duration - seconds) < .1 and duration <= 120
    assert len(info['streams']) == 1 and info['streams'][0]['codec_type'] == 'video'
    video = info['streams'][0]
    assert (video['width'], video['height']) == (1920, 1080)
    report = dict(video_file=output.name, duration=duration, width=1920, height=1080, audio=False, viewport=m['viewport'], viewport_placement={'x': 240, 'y': 140, 'width': 1440, 'height': 900}, transition_seconds=transition, timeline=timeline, sha256=digest(output))
    (base / 'learning-agent-stage3.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(duration, output)


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--manifest', type=Path, default=ROOT / 'assets/demo/stage3/timeline.json')
    p.add_argument('--output', type=Path, default=ROOT / '第三阶段Demo.mp4')
    a = p.parse_args()
    build(a.manifest, a.output)
