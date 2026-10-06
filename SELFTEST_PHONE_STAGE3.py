from pathlib import Path
import hashlib, json, py_compile, tempfile, sys, shutil, subprocess

ROOT = Path(__file__).resolve().parent
sys.dont_write_bytecode = True
checks=[]

def ck(name, cond, detail=''):
    checks.append((name, bool(cond), detail))

def sha(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()

expected={
 'app/src/main/python/live_core.py':'3bfdc6c03a2cb98305a1ff346d43bde73bde4163a81a9719dc4c2885c359912f',
 'app/src/main/python/live_database.py':'f0be6fc36aed9e10b7739b5d0e1c8aaa0cf9e6829aec88e3bcb5c2cb5fd8142f',
 'app/src/main/python/live_engine.py':'c41446bb0bfc4041f111085d78a7478f08b5d6c4c68dbe35c0f7b8f274c42e50',
 'app/src/main/assets/web/collector.js':'3b68c16559e97eb80cf901a8a640f4284d2125969931b2d992005569b9b7abb1',
}
for rel,h in expected.items():
    got=sha(ROOT/rel)
    ck('FROZEN '+rel, got==h, got)

compile_tmp=tempfile.mkdtemp(prefix='przewijak_compile_')
for rel in [
    'app/src/main/python/live_core.py','app/src/main/python/live_database.py',
    'app/src/main/python/live_engine.py','app/src/main/python/semantic_quality.py',
    'app/src/main/python/gold_quality.py','app/src/main/python/phone_core_bridge.py'
]:
    try:
        target=Path(compile_tmp)/(Path(rel).name+'.pyc')
        py_compile.compile(str(ROOT/rel), cfile=str(target), doraise=True)
        ck('PY COMPILE '+rel, True)
    except Exception as e:
        ck('PY COMPILE '+rel, False, str(e))
shutil.rmtree(compile_tmp, ignore_errors=True)

main=(ROOT/'app/src/main/java/pl/przewijak/live/MainActivity.kt').read_text('utf-8')
storage=(ROOT/'app/src/main/java/pl/przewijak/live/StorageAccess.kt').read_text('utf-8')
build=(ROOT/'app/build.gradle.kts').read_text('utf-8')
manifest=(ROOT/'app/src/main/AndroidManifest.xml').read_text('utf-8')
js=(ROOT/'app/src/main/assets/web/app.js').read_text('utf-8')
html=(ROOT/'app/src/main/assets/web/index.html').read_text('utf-8')
bridge=(ROOT/'app/src/main/python/phone_core_bridge.py').read_text('utf-8')

ck('Stage 3 version', 'PHONE-3.0.0-stage3' in build and 'PHONE-3.0.0-stage3' in main)
ck('No localhost runtime', 'localhost' not in main.lower() and '127.0.0.1' not in main)
ck('No Termux runtime', 'com.termux' not in main and 'python app.py' not in main)
ck('ACTION_OPEN_DOCUMENT_TREE', 'ACTION_OPEN_DOCUMENT_TREE' in storage)
ck('Persistable URI permission', 'takePersistableUriPermission' in storage and 'persistedUriPermissions' in storage)
ck('DocumentsContract copy', 'DocumentsContract.createDocument' in storage and 'buildChildDocumentsUriUsingTree' in storage)
ck('SD detection', 'isLikelySd' in storage and 'Karta SD' in storage)
ck('Manual export endpoint', '/api/storage/export' in main and 'EKSPORTUJ TERAZ' in html)
ck('Auto export on stop', 'autoExportEnabled' in main and 'auto_export_on_stop' in storage and 'Automatyczny eksport po STOP' in html)
ck('SQLite online backup', 'eng.db.conn.backup(dest)' in bridge)
ck('Export manifest', 'EXPORT_MANIFEST.json' in bridge)
ck('Private DB retained', 'source_of_truth_location' in bridge and 'app-private storage' in bridge)
ck('Collector bridge retained', '/api/heartbeat' in main and 'core.heartbeat' in main and '/api/ingest' in main and 'core.ingest' in main)
ck('Observer still gated', 'PHONE_STAGE3' in main and 'Warstwa Inteligentna pozostaje odseparowana' in main)
ck('PyApplication', 'com.chaquo.python.android.PyApplication' in manifest)

try:
    subprocess.run(['node','--check',str(ROOT/'app/src/main/assets/web/app.js')],check=True,capture_output=True,text=True)
    subprocess.run(['node','--check',str(ROOT/'app/src/main/assets/web/collector.js')],check=True,capture_output=True,text=True)
    ck('JavaScript syntax', True)
except Exception as e:
    ck('JavaScript syntax', False, str(e))

# Desktop smoke test of Python core + consistent export snapshot.
sys.path.insert(0, str(ROOT/'app/src/main/python'))
try:
    import phone_core_bridge as pcb
    td=tempfile.mkdtemp(prefix='przewijak_phone_stage3_')
    cd=tempfile.mkdtemp(prefix='przewijak_phone_export_')
    r=json.loads(pcb.init(td,5,False)); ck('CORE init smoke', r.get('ok') is True)
    r=json.loads(pcb.start()); ck('CORE start smoke', r.get('ok') is True and r.get('live',{}).get('running') is True)
    hb={"kind":"heartbeat","source_id":"fortuna","platform":"fortuna","source_label":"FORTUNA","parser_family":"fortuna","collector_id":"test","collector_session":"s","tab_id":"t"}
    r=json.loads(pcb.heartbeat(json.dumps(hb))); ck('Heartbeat smoke', r.get('ok') is True)
    ex=json.loads(pcb.prepare_export(cd))
    root=Path(ex.get('snapshot_dir',''))
    ck('Export snapshot smoke', ex.get('ok') is True and (root/'database'/'przewijak.sqlite3').exists())
    ck('Export manifest smoke', (root/'EXPORT_MANIFEST.json').exists())
    pcb.stop()
    shutil.rmtree(td, ignore_errors=True); shutil.rmtree(cd, ignore_errors=True)
except Exception as e:
    ck('CORE/export smoke exception', False, repr(e))

bad=[x for x in checks if not x[1]]
for n,ok,d in checks:
    print(('PASS' if ok else 'FAIL'), n, d)
print(f'\nWYNIK: {len(checks)-len(bad)}/{len(checks)} PASS')
raise SystemExit(1 if bad else 0)
