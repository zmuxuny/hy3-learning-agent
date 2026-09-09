import hashlib,json,subprocess,tarfile
from pathlib import Path
repo=Path('/root/workspace/tencent_rhinobird2026/learning_travel');r=Path(__file__).parent
rows=[]
paths=subprocess.check_output(['git','ls-tree','-r','--name-only','b2352dd','evaluation/artifacts','evaluation/releases','evaluation/schemas'],cwd=repo,text=True).splitlines()
mutable={'evaluation/releases/README.md','evaluation/artifacts/e6-completion-20260909/README.md'}
for name in paths:
 if name in mutable:continue
 old=subprocess.check_output(['git','show','b2352dd:'+name],cwd=repo);now=(repo/name).read_bytes();assert old==now,name;rows.append({'path':name,'sha256':hashlib.sha256(now).hexdigest()})
archive=repo/'evaluation/artifacts/e6-completion-20260909';manifest=json.loads((archive/'archive-manifest.json').read_text());assert hashlib.sha256((archive/'public-evidence.tar.gz').read_bytes()).hexdigest()==manifest['archive_sha256']
with tarfile.open(archive/'public-evidence.tar.gz') as tar:
 for row in manifest['files']:
  blob=tar.extractfile(row['path']).read();assert len(blob)==row['bytes'] and hashlib.sha256(blob).hexdigest()==row['sha256']
 ledger_before=tar.extractfile('budget-after-final.json').read()
ledger=Path('/root/.local/state/learning-travel/e4-hy3-20260905-osj4FCZZ-budget.json').read_bytes();assert ledger==ledger_before
result={'baseline':'b2352dd','immutable_assets_unchanged':True,'old_evidence_members_verified':len(manifest['files']),'old_archive_sha256':manifest['archive_sha256'],'ledger_byte_unchanged':True,'ledger_sha256':hashlib.sha256(ledger).hexdigest(),'paid_requests_added':0,'mutable_entry_documents':sorted(mutable),'files':rows}
(r/'historical-preservation.json').write_text(json.dumps(result,ensure_ascii=False,indent=2));print({k:v for k,v in result.items() if k!='files'},'assets',len(rows))
