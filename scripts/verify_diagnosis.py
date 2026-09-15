"""Registered full workbench verification for the request-diagnosis candidate."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import signal
import subprocess
import sys
import time

ROOT=Path(__file__).resolve().parents[1]


def classify(returncode,output,kind,expected=None):
    result={'status':'incomplete','executed':None,'failed':None,'skipped':0}
    if kind=='python':
        counts=[int(x) for x in re.findall(r'^Ran (\d+) tests? in ',output,re.M)]
        skips=sum(map(int,re.findall(r'skipped=(\d+)',output)))
        stability=len(re.findall(r'^  OK\s',output,re.M))
        stability_failed=len(re.findall(r'^  FAIL\s',output,re.M))
        failure_lines=re.findall(r'^FAILED[^\n]*',output,re.M)
        failed=stability_failed+sum(int(n) for line in failure_lines for n in re.findall(r'(?:failures|errors|unexpected successes)=(\d+)',line))
        known_failure=bool(failure_lines or re.search(r'^ERROR:|^FAIL:|^  FAIL\s',output,re.M))
        result.update(executed=sum(counts)+stability+stability_failed if counts or stability or stability_failed else None,skipped=skips,
                      failed=failed if failed else None if known_failure or returncode!=0 else 0)
        if known_failure:result['status']='failed'
        elif counts==[34,41,expected] and expected and stability==22 and skips==0 and 'All engine and integration checks passed.' in output:result['status']='passed'
    elif kind=='web':
        if 'UI contract tests passed:' in output:result.update(status='passed',executed=1,failed=0)
    elif kind=='e2e':
        result['executed']=output.count('Running ')
        if output.count(' passed: ')==5 and 'All five Mock CLI modes passed' in output:result.update(status='passed',executed=5,failed=0)
    elif kind=='legacy':
        result['executed']=len(re.findall(r'^Acceptance: ',output,re.M))
        if result['executed']==10:result.update(status='passed',failed=0)
    else:
        values=[]
        try:
            value=json.loads(output)
            if isinstance(value,dict):values.append(value)
        except ValueError:pass
        for line in output.splitlines():
            try:value=json.loads(line)
            except ValueError:continue
            if isinstance(value,dict):values.append(value)
        for value in values:
            if kind=='security' and value.get('files_checked',0)>0:
                result.update(executed=value['files_checked'],failed=len(value.get('findings',[])))
                result['status']='passed' if value.get('passed') is True and not value.get('findings') else 'failed'
            elif kind=='inspect' and value.get('requests_sent')==0 and value.get('fingerprint'):
                result.update(status='passed',executed=1,failed=0)
            elif kind=='browser' and value.get('status')=='passed' and value.get('mockRequests',0)>0:
                result.update(status='passed',executed=1,failed=0)
            elif kind=='json' and isinstance(value.get('checks'),int):
                result.update(executed=value['checks'],skipped=value.get('skipped',0))
                if value.get('status')=='passed' and value['checks']>0 and value.get('skipped')==0:result.update(status='passed',failed=0)
    if returncode is None and result['status']!='failed':result['status']='incomplete'
    elif returncode not in (None,0):
        result['status']='failed'
        if result['failed']==0:result['failed']=None
    return result


def execute(command,logpath,env,timeout):
    with logpath.open('w') as log:
        process=subprocess.Popen(command,cwd=ROOT,env=env,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
        try:return process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid,signal.SIGTERM)
            try:process.wait(timeout=5)
            except subprocess.TimeoutExpired:os.killpg(process.pid,signal.SIGKILL);process.wait()
            return None


def cleanup_container(py,env,output):
    try:
        cleanup=subprocess.run([py,'scripts/diagnosis_container.py','--output',str(output/'container'),'--cleanup'],
                               cwd=ROOT,env=env,capture_output=True,text=True,timeout=60)
        (output/'container-cleanup.log').write_text(cleanup.stdout+cleanup.stderr)
        return cleanup.returncode
    except (OSError,subprocess.TimeoutExpired):return None


def snapshot():
    files=subprocess.check_output(['git','ls-files','--cached','--others','--exclude-standard','-z'],cwd=ROOT).decode().split('\0')
    return {p:hashlib.sha256((ROOT/p).read_bytes()).hexdigest() for p in sorted(set(files)-{''}) if (ROOT/p).is_file()}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--sha',help='Require exact clean detached candidate and run legacy acceptance too')
    args=parser.parse_args();output=args.output.resolve()
    if output==ROOT or ROOT in output.parents:parser.error('output must be external')
    output.mkdir(parents=True,exist_ok=False)
    sha=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip()
    if args.sha and (args.sha!=sha or len(sha)!=40 or subprocess.check_output(['git','status','--porcelain'],cwd=ROOT).strip() or subprocess.check_output(['git','branch','--show-current'],cwd=ROOT).strip()):parser.error('acceptance requires exact clean detached SHA')
    before=snapshot();(output/'tmp').mkdir();(output/'platform').mkdir()
    # Use only process infrastructure plus explicitly selected test tooling; never inherit business configuration.
    allowed=('PATH','HOME','LANG','LC_ALL','SYSTEMROOT','SSL_CERT_FILE','SSL_CERT_DIR','PLAYWRIGHT_MODULE','PLAYWRIGHT_CHANNEL','PLAYWRIGHT_BROWSERS_PATH','DOCKER_HOST','DOCKER_CONTEXT')
    env={k:v for k,v in os.environ.items() if k in allowed}
    env.update(PYTHONDONTWRITEBYTECODE='1',PYTHONPATH=str(ROOT),TMPDIR=str(output/'tmp'),PLATFORM_DATA_DIR=str(output/'platform'),RELAY_LAB_DATA_DIR=str(output),PYTHON_EXECUTABLE=sys.executable,DIAGNOSIS_ENABLE_LIVE='0')
    py=sys.executable
    collect=[py,'-c',"import unittest; s=unittest.TestLoader().discover('tests'); print(s.countTestCases())"]
    cp=subprocess.run(collect,cwd=ROOT,env=env,capture_output=True,text=True,timeout=60)
    (output/'collection.log').write_text(cp.stdout+cp.stderr)
    expected=int(cp.stdout.strip()) if cp.returncode==0 and cp.stdout.strip().isdigit() else 0
    commands=[('workbench-python',[py,'scripts/test_all.py'],900,'python'),
              ('workbench-web',['node','scripts/test_web.js'],120,'web'),
              ('workbench-security',[py,'scripts/repo_security_scan.py','.'],120,'security'),
              ('syntax',[py,'scripts/diagnosis_syntax.py'],300,'json'),
              ('workbench-e2e',[py,'scripts/e2e.py','--output',str(output/'e2e')],600,'e2e'),
              ('workbench-browser',['node','scripts/ui_smoke.cjs'],600,'browser'),
              ('diagnosis-browser',['node','scripts/diagnosis_ui.cjs'],300,'json'),
              ('diagnosis-inspect',[py,'-m','features.diagnosis.inspect','--data-dir',str(output/'platform')],60,'inspect'),
              ('build-container',[py,'scripts/diagnosis_container.py','--output',str(output/'container')],900,'json')]
    if args.sha:commands.append(('legacy-acceptance',[py,'scripts/acceptance.py','--sha',sha,'--output',str(output/'legacy')],1200,'legacy'))
    results=[{'suite_id':name,'status':'not_run','command':cmd,'timeout_seconds':limit} for name,cmd,limit,kind in commands]
    result={'source_sha':sha,'source_files':before,'independent':bool(args.sha),'python':sys.version,'interpreter':py,'platform':platform.platform(),'expected_unittest_cases':expected,'rules_version':'1.0','results':results,'status':'incomplete','real_upstream_tested':False,'execution_budget_seconds':3600,'cleanup_grace_seconds':65}
    def save():
        temporary=output/'verification.tmp';temporary.write_text(json.dumps(result,ensure_ascii=False,indent=2));temporary.replace(output/'verification.json')
    save();began=time.monotonic()
    for row,(_,command,limit,kind) in zip(results,commands):
        if expected==0 or time.monotonic()-began>=3600:break
        print('Verify: '+row['suite_id'],flush=True);start=time.monotonic();logpath=output/(row['suite_id']+'.log')
        code=execute(command,logpath,env,min(limit,3600-(time.monotonic()-began)))
        text=logpath.read_text(errors='replace')
        row.update(classify(code,text,kind,expected))
        row.update(exit_code=code,elapsed_seconds=time.monotonic()-start,log=str(logpath))
        if row['suite_id']=='build-container':
            row['cleanup_exit_code']=cleanup_container(py,env,output)
            if row['cleanup_exit_code'] is None and row['status']!='failed':row['status']='incomplete'
            elif row['cleanup_exit_code']!=0:row['status']='failed'
        if kind=='legacy' and row['status']=='passed':
            artifact=output/'legacy'/'acceptance.json'
            if not artifact.exists() or json.loads(artifact.read_text()).get('passed') is not True:row['status']='incomplete'
        save()
    result['source_unchanged']=before==snapshot()
    result['status']='failed' if any(r['status']=='failed' for r in results) else 'passed' if all(r['status']=='passed' for r in results) and result['source_unchanged'] and expected>0 else 'incomplete'
    result['elapsed_seconds']=time.monotonic()-began;save()
    print(json.dumps({'status':result['status'],'evidence':str(output/'verification.json')}))
    return 0 if result['status']=='passed' else 1


if __name__=='__main__':sys.exit(main())
