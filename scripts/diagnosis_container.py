import argparse
import json
from pathlib import Path
import re
import subprocess
import time
import uuid

ROOT=Path(__file__).resolve().parents[1]


def docker(*args,**kwargs):
    return subprocess.run(['docker',*args],check=True,text=True,**kwargs)


def cleanup(output):
    owner=output/'owner.json'
    if not owner.exists():return 'not_created'
    identifier=json.loads(owner.read_text())['identifier']
    if not re.fullmatch(r'diagnosis-test-[0-9a-f]{12}',identifier):raise ValueError('invalid ownership record')
    cp=subprocess.run(['docker','container','ls','-a','--filter','name=^/'+identifier+'$','--format','{{.Names}}'],check=True,text=True,capture_output=True,timeout=15)
    if identifier not in cp.stdout.splitlines():return 'absent'
    inspected=json.loads(docker('inspect',identifier,capture_output=True,timeout=15).stdout)[0]
    if inspected.get('Config',{}).get('Labels',{}).get('diagnosis.owner')!=identifier:
        raise ValueError('container ownership mismatch')
    docker('rm','-f',identifier,timeout=20)
    return 'removed'


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True);parser.add_argument('--cleanup',action='store_true');args=parser.parse_args()
    output=args.output.resolve()
    if args.cleanup:
        print(json.dumps({'cleanup':cleanup(output)}));return
    output.mkdir(parents=True,exist_ok=False)
    identifier='diagnosis-test-'+uuid.uuid4().hex[:12];image=identifier+':local'
    (output/'owner.json').write_text(json.dumps({'identifier':identifier}))
    sha=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip()
    try:
        docker('build','--label',f'diagnosis.source={sha}','-t',image,str(ROOT),timeout=600)
        docker('run','-d','--name',identifier,'--label',f'diagnosis.owner={identifier}','--tmpfs','/app/data:uid=10001,gid=10001,mode=0700',
               '-e','PLATFORM_USERNAME=fixture','-e','PLATFORM_PASSWORD=synthetic-container-password',
               '-e','DIAGNOSIS_ENABLE_LIVE=0',image,timeout=30)
        for _ in range(90):
            state=json.loads(docker('inspect',identifier,capture_output=True).stdout)[0]['State']
            if state.get('Health',{}).get('Status')=='healthy':break
            if not state['Running']:raise RuntimeError('owned container stopped')
            time.sleep(1)
        else:raise RuntimeError('container health timeout')
        code="""import base64,json,urllib.request
def request(path,body=None):
 r=urllib.request.Request('http://127.0.0.1:8090'+path,data=json.dumps(body).encode() if body is not None else None,headers={'Authorization':'Basic '+base64.b64encode(b'fixture:synthetic-container-password').decode(),'Content-Type':'application/json'})
 return urllib.request.urlopen(r,timeout=10).read()
assert b'REQUEST DIAGNOSTICS' in request('/diagnosis/')
assert b'chooseCase' in request('/diagnosis/static/app.js')
assert json.loads(request('/diagnosis/api/config'))['live_enabled'] is False
case=json.loads(request('/diagnosis/api/cases',{'cases':[{'total_tokens':100,'stream':True}]}))[0]
p=json.loads(request('/diagnosis/api/preview',{'case_id':case['id'],'repetitions':1,'variants':[]}))
r=json.loads(request('/diagnosis/api/runs',{'preview_id':p['preview_id']}))
import time
for _ in range(100):
 value=json.loads(request('/diagnosis/api/runs/'+r['id']))
 if value['run']['state']!='running':break
 time.sleep(.1)
assert value['run']['results'][0]['outcome']=='completed'
assert json.loads(request('/api/health'))['status']=='ok'
"""
        docker('exec',identifier,'python','-c',code,timeout=30)
        image_id=docker('image','inspect',image,'--format','{{.Id}}',capture_output=True).stdout.strip()
        result={'status':'passed','checks':6,'skipped':0,'source_sha':sha,'image_id':image_id,'published':False}
        (output/'container.json').write_text(json.dumps(result,indent=2))
        print(json.dumps(result))
    finally:
        cleanup(output)
        # Keep the uniquely tagged local image as versioned evidence; no registry push.


if __name__=='__main__':main()
