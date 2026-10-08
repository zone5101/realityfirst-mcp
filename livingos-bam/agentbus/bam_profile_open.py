from __future__ import annotations
import json, os, sys, time, uuid, urllib.request, urllib.parse
from datetime import datetime, timezone
from pathlib import Path

REQUEST_DIR=Path(os.environ.get('BAM_PROFILE_UI_REQUEST_DIR') or r'G:\AgentBusProfiles\_bam_ui_profile_requests')
REGISTRY=Path(r'G:\AgentBusProfiles\chrome_profile_registry.json')
CONTAINMENT=Path(r'G:\AgentBusProfiles\chrome_containment_state.json')

def _atomic_write(path: Path, value: dict) -> None:
    tmp=path.with_suffix(path.suffix+'.tmp')
    tmp.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    os.replace(tmp,path)

def _profile_row(profile: str) -> dict:
    doc=json.loads(REGISTRY.read_text(encoding='utf-8-sig'))
    if doc.get('schema')!='livingos.chrome-profile-registry.v2': raise SystemExit('REGISTRY_SCHEMA')
    row=next((dict(x) for x in doc.get('profiles') or [] if str(x.get('id'))==profile),None)
    if not row: raise SystemExit('UNKNOWN_PROFILE')
    return row

def _listener_pid(port: int) -> int | None:
    import psutil
    for conn in psutil.net_connections(kind='tcp'):
        try:
            if getattr(conn,'pid',None) and getattr(getattr(conn,'laddr',None),'port',None)==int(port) and str(getattr(conn,'status','')).upper()=='LISTEN':
                return int(conn.pid)
        except Exception:
            continue
    return None

def _open_utility_tab(port: int, url: str) -> str:
    allowed=('chrome://extensions','chrome://settings','chrome://extensions/shortcuts')
    if not any(url==x or url.startswith(x+'/') for x in allowed):
        raise SystemExit('UTILITY_URL_NOT_ALLOWED')
    req=urllib.request.Request(f'http://127.0.0.1:{int(port)}/json/new?'+urllib.parse.quote(url,safe=':/'),method='PUT')
    with urllib.request.urlopen(req,timeout=3) as response:
        row=json.loads(response.read().decode('utf-8'))
    actual=str(row.get('url') or '')
    if not actual.startswith('chrome://'):
        raise SystemExit('UTILITY_TAB_NOT_PROVEN')
    return actual

def _record_contained(profile: str, surface_class: str) -> dict:
    row=_profile_row(profile); pid=_listener_pid(int(row['cdp_port']))
    if not pid: raise SystemExit('CONTAINMENT_CDP_OWNER_MISSING')
    try: state=json.loads(CONTAINMENT.read_text(encoding='utf-8-sig'))
    except Exception: state={'schema':'livingos.chrome-containment-state.v1','profiles':{}}
    if state.get('schema')!='livingos.chrome-containment-state.v1': state={'schema':'livingos.chrome-containment-state.v1','profiles':{}}
    state.setdefault('profiles',{})[profile]={
        'profile':profile,'pid':pid,'cdp_port':int(row['cdp_port']),
        # Separation (2026-10-08): Chrome is no longer contained/owned by BAM.  The window is
        # a top-level Chrome window and the runtime is owned by the Chrome fleet owner
        # (task LivingOS-Chrome-Profile-Fleet); BAM attaches as a CDP client only.
        'contained':False,'host':'CHROME_FLEET','window_owner':'chrome','bam_role':'CDP_CLIENT',
        'surface_classes':['WORKER_POOL','UTILITY_ADMIN'],
        'last_surface_class':surface_class,
        'observed_at':datetime.now(timezone.utc).isoformat(),
    }
    _atomic_write(CONTAINMENT,state)
    return dict(state['profiles'][profile])

def main() -> int:
    if len(sys.argv) not in (2,3,4): raise SystemExit('USAGE: bam_profile_open.py <accNN> [WORKER_POOL|UTILITY_ADMIN] [utility_url]')
    profile=str(sys.argv[1]).strip()
    surface_class=str(sys.argv[2] if len(sys.argv)>=3 else 'HUMAN_OPERATOR').strip().upper()
    utility_url=str(sys.argv[3] if len(sys.argv)==4 else 'chrome://extensions').strip()
    if surface_class not in {'HUMAN_OPERATOR','UTILITY_ADMIN'}: raise SystemExit('BAD_SURFACE_CLASS')
    row=_profile_row(profile)
    REQUEST_DIR.mkdir(parents=True,exist_ok=True)
    rid=f'{int(time.time()*1000)}-{uuid.uuid4().hex}'
    req=REQUEST_DIR/(rid+'.json'); result=REQUEST_DIR/(rid+'.result.json')
    req.write_text(json.dumps({'profile':profile,'request_id':rid,'surface_class':surface_class,'action':'open','interactive':True}),encoding='utf-8')
    deadline=time.time()+30
    while time.time()<deadline:
        if result.exists():
            row=json.loads(result.read_text(encoding='utf-8')); result.unlink(missing_ok=True)
            if row.get('status')!='ATTACHED': raise SystemExit(row.get('error') or 'BAM_UI_ATTACH_FAILED')
            containment=_record_contained(profile,surface_class)
            utility=None
            if surface_class=='UTILITY_ADMIN': utility=_open_utility_tab(int(containment['cdp_port']),utility_url)
            print(json.dumps({**row,'containment':containment,'utility_url':utility},ensure_ascii=False)); return 0
        time.sleep(0.1)
    req.unlink(missing_ok=True)
    raise SystemExit('BAM_UI_NOT_RESPONDING')

if __name__=='__main__': raise SystemExit(main())
