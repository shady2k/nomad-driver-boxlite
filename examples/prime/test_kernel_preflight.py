import test_support
import importlib.util,subprocess,unittest
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace
spec=importlib.util.spec_from_file_location('kernel_bootstrap',Path(__file__).parent/'guest/bootstrap.py');boot=importlib.util.module_from_spec(spec);spec.loader.exec_module(boot)
class KernelPreflight(unittest.TestCase):
 def test_exact_default_imports_checked_without_credentials(self):
  env={'PATH':'/usr/bin:/bin','HOME':'/root'}
  with patch.object(boot.subprocess,'run',return_value=SimpleNamespace(returncode=0)) as run:
   self.assertTrue(boot.kernel_preflight(env));args,kwargs=run.call_args
   self.assertEqual(kwargs['env'],env);self.assertNotIn('LLM_API_KEY',kwargs['env']);self.assertEqual(kwargs['timeout'],30)
   for name in ['requests','httpx','yaml','tomli','dotenv','pandas','numpy','scipy','bs4','lxml','pydantic','tyro']:self.assertIn(repr(name),args[0][-1])
 def test_import_failure_refuses_readiness(self):
  with patch.object(boot.subprocess,'run',return_value=SimpleNamespace(returncode=1)):self.assertFalse(boot.kernel_preflight({}))
 def test_unavailable_or_timeout_is_not_ready(self):
  for error in [FileNotFoundError(),subprocess.TimeoutExpired('fixture',30)]:
   with patch.object(boot.subprocess,'run',side_effect=error):self.assertFalse(boot.kernel_preflight({}))

class DockerPreflight(unittest.TestCase):
 def test_legacy_image_does_not_start_docker(self):
  with patch.object(boot.Path,'is_file',return_value=False),patch.object(boot.subprocess,'run') as run:
   self.assertTrue(boot.docker_preflight({}));run.assert_not_called()
 def test_image_default_uses_fixed_guest_script_without_secrets(self):
  env={'PATH':'/usr/bin:/bin','HOME':'/root'}
  with patch.object(boot.Path,'is_file',return_value=True),patch.object(boot.subprocess,'run',return_value=SimpleNamespace(returncode=0)) as run:
   self.assertTrue(boot.docker_preflight(env));args,kw=run.call_args
   self.assertEqual(args[0],['/opt/worker/start-docker.sh']);self.assertEqual(kw['env'],env);self.assertEqual(kw['stdin'],subprocess.DEVNULL)
 def test_failed_or_uncertain_daemon_is_not_ready(self):
  with patch.object(boot.Path,'is_file',return_value=True):
   with patch.object(boot.subprocess,'run',return_value=SimpleNamespace(returncode=70)):self.assertFalse(boot.docker_preflight({}))
   for err in [FileNotFoundError(),subprocess.TimeoutExpired('fixture',40)]:
    with patch.object(boot.subprocess,'run',side_effect=err):self.assertFalse(boot.docker_preflight({}))
