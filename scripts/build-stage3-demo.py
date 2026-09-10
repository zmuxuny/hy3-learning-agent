"""Build the application + evaluation Demo with full browser viewports intact.

Requires ffmpeg, Pillow, and Noto Sans CJK. Reads archived footage; no API calls.
"""
import argparse,json,subprocess,tempfile,hashlib
from pathlib import Path
from PIL import Image,ImageDraw,ImageFont
ROOT=Path(__file__).resolve().parents[1]
FONT='/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc'
BOLD='/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc'
def slide(path,title,lines):
    im=Image.new('RGB',(1920,1080),'#f5f8f4');d=ImageDraw.Draw(im)
    d.text((135,90),'Learning Agent · Hy3 / 个人活动作品',font=ImageFont.truetype(FONT,28),fill='#586e62')
    d.text((130,185),title,font=ImageFont.truetype(BOLD,57),fill='#244f41')
    y=335
    for line in lines:
        font=ImageFont.truetype(FONT,39)
        assert d.textbbox((0,0),line,font=font)[2]<1670,line
        d.text((135,y),line,font=font,fill='#263a30');y+=108
    d.line((135,932,1785,932),fill='#bfcfc4',width=2)
    d.text((135,962),'任务一：面向真实场景的应用 + 自定义评测 + 实验验证',font=ImageFont.truetype(FONT,29),fill='#596e62');im.save(path)
def ff(*args):subprocess.run(['ffmpeg','-v','error','-y',*map(str,args)],check=True)
def build(manifest,output):
    m=json.loads(manifest.read_text());base=manifest.parent
    for p,h in m['source_sha256'].items():assert hashlib.sha256((base/p).read_bytes()).hexdigest()==h
    pages={
      'intro':('从学习目标，到有依据的下一步',['面向编程自学者：计划、作品反馈与过程支持。','Hy3理解条件并调用工具，用户审阅后采用计划。','我们也评价助手：建议正确吗？行动合适吗？']),
      'method':('评价一次决策，需要看到完整证据',['用户要求 + 助手内容 + 工具结果 + 前后状态','七维：事实、目标、时机、约束、可用性、适度、解释','内容核验与程序检查 → 自动评分 → 逐例复核','接下来：两周卷积学习计划里的边界条件遗漏。']),
      'closing':('应用、数据与评价依据一起交付',['48个真实应用场景：复核46通过、2未通过。','24份三档输出各评3次：严格排序22 / 24组。','加权分标准差均值4.56；评分操纵误通过0 / 8。','图文报告 · 原始结果与复核 · 数据集 · 离线复算','github.com/zmuxuny/hy3-learning-agent'])}
    if 'extension_evidence_archive' in m:
        summary_path=ROOT/m['extension_evidence_archive']/'automatic/summary.json'
        assert hashlib.sha256(summary_path.read_bytes()).hexdigest()==m['extension_summary_sha256']
        summary=json.loads(summary_path.read_text())
        pages['closing']=('应用、数据与评价依据一起交付',[
            '48个真实应用场景：人工确认46通过、2未通过。',
            f"新增受控验证：严重{summary['severe_identified_critical']}/{summary['severe_slots']}识别，正常{summary['good_passed']}/{summary['good_slots']}通过。",
            '双人人工盲标首轮128个位置，逐项一致。',
            '首轮三档严格排序22/24；图文报告与完整结果可复算。',
            'github.com/zmuxuny/hy3-learning-agent'])
    output.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='stage3-video-') as tmp:
        tmp=Path(tmp);parts=[];seconds=0;timeline=[]
        for i,s in enumerate(m['segments']):
            dest=tmp/f'{i}.mp4';duration=s['duration'];timeline.append({**s,'video_start':seconds,'video_end':seconds+duration});seconds+=duration
            common=['-an','-t',duration,'-r','25','-c:v','libx264','-preset','fast','-crf','20','-pix_fmt','yuv420p']
            if s['type']=='clip':
                caption=tmp/f'{i}.txt';caption.write_text(s['caption'])
                # Scale the entire 1440x900 viewport; captions occupy the added top margin.
                vf=f"scale=1600:1000,pad=1920:1080:160:80:white,tpad=stop_mode=clone:stop_duration=2,drawtext=fontfile={FONT}:textfile={caption}:x=160:y=20:fontsize=32:fontcolor=0x244f41"
                ff('-ss',s['start'],'-i',base/s['source'],'-vf',vf,*common,dest)
            else:
                img=tmp/f'{i}.png';slide(img,*pages[s['type']]);ff('-loop','1','-i',img,*common,dest)
            parts.append(dest)
        listing=tmp/'concat.txt';listing.write_text(''.join("file '"+str(p)+"'\n" for p in parts));ff('-f','concat','-safe','0','-i',listing,'-c','copy','-movflags','+faststart',output)
    info=json.loads(subprocess.check_output(['ffprobe','-v','error','-show_streams','-show_format','-of','json',str(output)]));duration=float(info['format']['duration']);assert duration<=120
    video=next(s for s in info['streams'] if s['codec_type']=='video');assert (video['width'],video['height'])==(1920,1080)
    report=dict(duration=duration,width=1920,height=1080,viewport=m['viewport'],timeline=timeline,sha256=hashlib.sha256(output.read_bytes()).hexdigest())
    output.with_suffix('.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n');print(duration,output)
if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--manifest',type=Path,default=ROOT/'assets/demo/stage3/timeline.json');p.add_argument('--output',type=Path,default=ROOT/'assets/demo/stage3/learning-agent-stage3.mp4');a=p.parse_args();build(a.manifest,a.output)
