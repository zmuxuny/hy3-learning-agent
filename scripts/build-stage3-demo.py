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
    im=Image.new('RGB',(1920,1080),'#f7f8fa');d=ImageDraw.Draw(im)
    d.text((135,90),'Learning Agent · Hy3 / 个人活动作品',font=ImageFont.truetype(FONT,28),fill='#647185')
    d.text((130,185),title,font=ImageFont.truetype(BOLD,57),fill='#203d63')
    y=335
    for line in lines:
        font=ImageFont.truetype(FONT,39)
        assert d.textbbox((0,0),line,font=font)[2]<1670,line
        d.text((135,y),line,font=font,fill='#283343');y+=108
    d.line((135,932,1785,932),fill='#d4dce6',width=2)
    d.text((135,962),'任务一：面向真实场景的应用 + 自定义评测 + 实验验证',font=ImageFont.truetype(FONT,29),fill='#647185');im.save(path)
def ff(*args):subprocess.run(['ffmpeg','-v','error','-y',*map(str,args)],check=True)
def build(manifest,output):
    m=json.loads(manifest.read_text());base=manifest.parent
    for p,h in m['source_sha256'].items():assert hashlib.sha256((base/p).read_bytes()).hexdigest()==h
    pages={
      'intro':('从学习目标，到有依据的下一步',['面向编程自学者：计划、作品反馈与过程支持。','Hy3理解条件并调用工具，用户审阅后采用计划。','评测同时核查建议内容、行动效果与用户控制。']),
      'method':('评价一次决策，需要看到完整证据',['用户要求 + 助手内容 + 工具结果 + 前后状态','七维：事实、目标、时机、约束、可用性、适度、解释','内容核验与程序检查 → 自动评分 → 逐例复核','接下来：两周卷积学习计划里的边界条件遗漏。']),
      'closing':('应用、数据与评价依据一起交付',[])}
    summary_path=ROOT/m['evidence_archive']/'automatic/summary.json'
    assert hashlib.sha256(summary_path.read_bytes()).hexdigest()==m['summary_sha256']
    summary=json.loads(summary_path.read_text());n=summary['parts']['new_scenarios']
    pages['closing']=('应用、数据与评价依据一起交付',[
        '48个真实应用场景：人工参考46通过、2未通过。',
        f"三档严格排序 {summary['discrimination']['strict_raw']}/24；评分操纵误通过 {summary['parts']['adversarial']['passed']}/8。",
        f"正常与严重对照：正常{n['good_passed']}/{n['good_total']}通过，严重{n['severe_critical_failed']}/{n['severe_total']}识别。",
        '116份输入 · 主体200次 + 条件专项36次评分',
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
                vf=f"scale=1600:1000,pad=1920:1080:160:80:white,tpad=stop_mode=clone:stop_duration=2,drawtext=fontfile={FONT}:textfile={caption}:x=160:y=20:fontsize=32:fontcolor=0x203d63"
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
