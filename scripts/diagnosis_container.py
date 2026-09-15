import argparse
import json
from pathlib import Path
import subprocess
import time
import uuid

ROOT=Path(__file__).resolve().parents[1]


def docker(*args,**kwargs):
    return subprocess.run(['docker',*args],check=True,text=True,**kwargs)


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True);args=parser.parse_args()
    output=args.output.resolve();output.mkdir(parents=True,exist_ok=False)
    identifier='diagnosis-test-'+uuid.uuid4().hex[:12];image=identifier+':local';created=False
    sha=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip()
    try:
        docker('build','--label',f'diagnosis.source={sha}','-t',image,str(ROOT),timeout=600)
        docker('run','-d','--name',identifier,'--tmpfs','/app/data:uid=10001,gid=10001,mode=0700',
               '-e','PLATFORM_USERNAME=fixture','-e','PLATFORM_PASSWORD=synthetic-container-password',
               '-e','DIAGNOSIS_ENABLE_LIVE=0',image,timeout=30)
        created=True
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
        if created:docker('rm','-f',identifier,timeout=30)
        # Keep the uniquely tagged local image as versioned evidence; no registry push.


if __name__=='__main__':main()
