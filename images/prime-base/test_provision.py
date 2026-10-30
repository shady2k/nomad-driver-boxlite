import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[2] / 'examples/prime/guest/provision.sh'

class ArchitectureSelection(unittest.TestCase):
    def select(self, architecture):
        # Execute only the architecture selector. It precedes OS/file/network work.
        source = SCRIPT.read_text().split('. /etc/os-release', 1)[0]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'uname'
            path.write_text('#!/bin/sh\nprintf "%s\\n" '+architecture+'\n')
            path.chmod(0o700)
            return subprocess.run(['/bin/sh', '-c', source+'\nprintf "%s %s %s %s\\n" "$prime_arch" "$node_arch" "$prime_sha" "$uv_url"'], env={'PATH':directory+':/usr/bin:/bin'}, text=True, capture_output=True)
    def test_amd64_uses_official_x64_artifacts(self):
        result=self.select('x86_64');self.assertEqual(result.returncode,0)
        self.assertIn('x64 x64 21ec219183f4134dd23dce9572102e69456066fe374846525561c776d4d1945c',result.stdout)
        self.assertIn('manylinux_2_17_x86_64',result.stdout)
    def test_arm64_retains_existing_artifacts(self):
        result=self.select('aarch64');self.assertEqual(result.returncode,0)
        self.assertIn('arm64 arm64 5af68c10a088538fd4990fd18778d8c7fe28c0491083dc069c5177f733770e91',result.stdout)
        self.assertIn('manylinux_2_28_aarch64',result.stdout)
    def test_unsupported_arch_fails_before_files_or_network(self):
        result=self.select('riscv64');self.assertEqual(result.returncode,64)
        self.assertIn('Unsupported guest architecture',result.stderr)
    def test_shell_syntax(self):
        subprocess.run(['/bin/sh','-n',str(SCRIPT)],check=True)

if __name__=='__main__':unittest.main()
