import importlib.util
from pathlib import Path
import pytest

spec = importlib.util.spec_from_file_location('fork_diagnostic', Path(__file__).parents[1] / 'tools' / 'fork_diagnostic.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

def test_sequence_requires_every_call_to_succeed():
    assert module.check_results([{'status':'0x1','gasUsed':'0x10'},{'status':'0x1','gasUsed':'0x20'}],2)==[16,32]
    with pytest.raises(RuntimeError):
        module.check_results([{'status':'0x1','gasUsed':'0x10'},{'status':'0x0','gasUsed':'0x20'}],2)
    with pytest.raises(RuntimeError):
        module.check_results([{'status':'0x1','gasUsed':'0x10'}],2)
