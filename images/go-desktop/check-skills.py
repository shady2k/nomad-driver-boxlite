import importlib,json,pathlib,tomllib
modules=[]
for project in pathlib.Path('/opt/worker/prime/skills').glob('*/pyproject.toml'):
 for module in (project.parent/'src').glob('*/__init__.py'):
  name=module.parent.name;loaded=importlib.import_module(name);modules.append({'module':name,'path':loaded.__file__})
expected={'agent_observe','agent_message','edit','goal','attach_image','compact','refine','rlm_heartbeat'}
assert expected.issubset({x['module'] for x in modules})
pathlib.Path('/opt/worker-build-meta/prime-skills.json').write_text(json.dumps(modules));print(json.dumps({'kernel_skills_imported':len(modules),'modules':[x['module'] for x in modules]}))
