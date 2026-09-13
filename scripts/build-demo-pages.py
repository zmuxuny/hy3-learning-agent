"""Build the demo site with results computed from archived CSV/JSON evidence."""
import argparse
import csv
import json
import shutil
from pathlib import Path
from statistics import mean

ROOT = Path(__file__).resolve().parents[1]
ARCHIVE = ROOT / 'evaluation/artifacts/decisionbench-final-method-20260910/automatic'
START, END = '<!-- BEGIN EVALUATION RESULTS -->', '<!-- END EVALUATION RESULTS -->'


def build(output):
    rows = list(csv.DictReader((ARCHIVE / 'application-human.csv').open()))
    summary = json.loads((ARCHIVE / 'summary.json').read_text())
    total = len(rows)
    human_pass = sum(r['human_outcome'] == 'pass' for r in rows)
    auto_pass = sum(r['automatic_outcome'] == 'pass' for r in rows)
    matches = sum(r['automatic_outcome'] == r['human_outcome'] for r in rows)
    rank = summary['discrimination']
    table = ''
    for track, label in [('planning', '学习规划'), ('intervention', '主动介入'), ('assessment', '成果验收'), ('revision', '计划调整')]:
        group = [r for r in rows if r['track'] == track]
        table += f'<tr><th scope="row">{label}</th><td>{len(group)}</td><td>{mean(float(r["automatic_score"]) for r in group):.1f}</td><td>{mean(float(r["human_score"]) for r in group):.1f}</td></tr>'
    section = f'''<section class="results" id="results" aria-labelledby="results-title">
      <header class="results-heading"><p class="eyebrow">评测结果</p><h2 id="results-title">助手的表现，以及评测方法的有效性</h2>
      <p>应用实验检查助手处理学习任务的表现；方法验证检查评测器能否辨别决策质量。以下结果来自同一套已归档实验。</p></header>
      <div class="results-columns">
        <article><h3>应用实验 · {total}项任务</h3>
          <p>学习规划、主动介入、成果验收、计划调整各12项，每项实际运行一次。评分结合用户条件、助手回复、工具结果与状态变化，满分100分。</p>
          <div class="score-table"><table><caption>四类决策的平均分</caption><thead><tr><th scope="col">决策类型</th><th scope="col">任务数</th><th scope="col">自动评分</th><th scope="col">人工参考</th></tr></thead><tbody>{table}</tbody></table></div>
          <p class="result-note">自动评分达标 {auto_pass}/{total} 项，人工参考达标 {human_pass}/{total} 项。最终分达到70记为决策评分达标；这评价的是助手的判断，正确退回失败作品也可获得高分。</p>
          <a href="./results/application-human.csv" download>下载48项任务的逐例评分（CSV）</a>
        </article>
        <article><h3>方法验证 · 识别质量与对齐人工判断</h3>
          <div class="finding"><p class="finding-number">{rank['strict_final']} / {rank['total']}<span>质量排序正确</span></p>
          <p>选8个情境，各编写正确处理、局部缺陷、严重错误三个版本，共24份固定记录；每份评分3次，共72次评分。每个情境每次评分形成一组排序，合计24组，其中{rank['strict_final']}组按质量依次降分。</p></div>
          <div class="finding"><p class="finding-number">{matches} / {total}<span>自动与人工达标结论一致</span></p>
          <p>对同一批48项应用任务，比较自动评分与两位标注者协商后的参考结论。{matches}项对“助手决策是否达标”的判断相同，一致率为{matches/total:.1%}。</p></div>
          <a href="./results/triplets.csv" download>下载24组质量排序结果（CSV）</a>
        </article>
      </div>
      <div class="results-links"><a class="report-link" href="./report.html">阅读完整图文报告 →</a><a href="https://github.com/zmuxuny/hy3-learning-agent/blob/main/评测用例目录.md">查看116项评测材料</a><a href="https://github.com/zmuxuny/hy3-learning-agent/tree/main/交付材料">数据、人工标注与复算命令</a></div>
    </section>'''
    html = (ROOT / 'pages/index.html').read_text()
    before, rest = html.split(START, 1)
    _, after = rest.split(END, 1)
    output.mkdir(parents=True, exist_ok=True)
    (output / 'index.html').write_text(before + START + '\n' + section + '\n' + END + after)
    (output / 'media').mkdir(exist_ok=True)
    for source, destination in [('assets/demo/stage3/cover.png', 'media/cover.png'), ('assets/brand/learning-agent-mark.svg', 'media/learning-agent-mark.svg'), ('第三阶段Demo.mp4', 'media/learning-agent-stage3.mp4'), ('第三阶段项目与评测报告.html', 'report.html')]:
        shutil.copy2(ROOT / source, output / destination)
    (output / 'results').mkdir(exist_ok=True)
    for name in ['application-human.csv', 'triplets.csv', 'summary.json']:
        shutil.copy2(ARCHIVE / name, output / 'results' / name)
    print(f'Demo site built: {output}; {total} application cases, {rank["total"]} quality comparisons')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    build(parser.parse_args().output)
