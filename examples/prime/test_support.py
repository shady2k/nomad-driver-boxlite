"""Neutral test-only operator bindings. Never imported by production."""
import os
from pathlib import Path
os.environ['BOXLITE_BINDINGS_FILE']=str(Path(__file__).parent/'testdata/legacy-bindings.json')
