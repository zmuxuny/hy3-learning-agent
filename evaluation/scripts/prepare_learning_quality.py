"""Verify archived run linkages and export label-free scenario evidence."""
import argparse,json,shutil
from pathlib import Path
from evaluate_e7_trace import verify_bundle
from learning_agent_eval.blinding_v2 import build_blind_judge_input_v3
from learning_agent_eval.learning_quality import projection,request_for
from learning_agent_eval.canonical import canonical_json_bytes,sha256_digest
from learning_agent_eval.integrity import artifact_manifest_digest

def export(run,out,kind):
    out.mkdir(parents=True,exist_ok=False);(out/'evidence').mkdir();(out/'sources').mkdir()
    runtime=json.loads((run/'runtime/run-manifest.json').read_text());rules=json.loads((run/'rules/rule-manifest.json').read_text())
    assert artifact_manifest_digest(runtime)==runtime['manifest_sha256']
    assert artifact_manifest_digest(rules)==rules['manifest_sha256']
    (out/'sources/runtime-manifest.json').write_bytes(canonical_json_bytes(runtime))
    (out/'sources/rule-manifest.json').write_bytes(canonical_json_bytes(rules))
    specs=[];sizes=[]
    for terminal in runtime['terminals']:
        row=dict(id=terminal['case_id'],track=terminal['track'],kind=kind,source_terminal=terminal)
        if terminal['terminal_kind']=='episode':
            identity=terminal['artifact_id'];e=json.loads((run/f'runtime/episodes/{identity}.json').read_text())
            r=json.loads((run/f'rules/rules/{identity}.json').read_text());ref=json.loads((run/f'runtime/judge-references/{identity}.json').read_text())
            verify_bundle(terminal,e,r,ref,runtime,rules)
            public=projection(build_blind_judge_input_v3(e,r,ref).document)
            file=f"evidence/{row['id']}.json";(out/file).write_bytes(canonical_json_bytes(public))
            for label,item in [('episode',e),('rules',r),('reference',ref)]:
                (out/f"sources/{row['id']}-{label}.json").write_bytes(canonical_json_bytes(item))
            row.update(evidence_file=file,evidence_sha256=sha256_digest(public),source_episode_sha256=e['provenance']['episode_sha256'])
            sizes.append(len(canonical_json_bytes(request_for(public)))+2048)
        specs.append(row)
    (out/'suite.json').write_bytes(canonical_json_bytes(specs))
    print(out,len(specs),'evidence',len(sizes),'request bytes max',max(sizes))

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--run',type=Path,required=True);p.add_argument('--output',type=Path,required=True);p.add_argument('--kind',required=True)
    a=p.parse_args();export(a.run,a.output,a.kind)
