"""Import intent, identity ownership and early failure recovery boundaries."""
import json
import os
from pathlib import Path

import pytest

from maestro.backends import control_files, git_objects, git_staging
from tests.test_git_staging import linked, git
from tests.test_git_object_validation import staged

pytestmark = pytest.mark.real_fsync


def test_durable_intent_before_destination_creation_is_recoverable(linked):
    _,tree,reports,_=linked
    transaction=git_staging.Transaction.prepare(tree,reports)
    pack=transaction.common/'objects/pack'
    pack.rmdir()
    control_files.write(transaction.directory/'object-import.json',json.dumps(
        dict(pack='pack-'+'a'*40,journal=transaction.job,temporary=[],keep=None)),durable=True)
    try:
        with git_staging._guard(transaction.common/'maestro-native/object-publication'):
            git_objects.release_keep(transaction)
        assert not pack.exists()
    finally:transaction.cleanup()


def test_same_bytes_replacement_keep_is_not_owned_by_receipt(linked):
    transaction=staged(linked)
    view=git_objects.ObjectValidation(transaction)
    tip=(transaction.directory/transaction.ref).read_text().strip()
    view.validate(tip)
    try:
        with git_staging._guard(transaction.common/'maestro-native/object-publication'):
            view.publish()
            keep=transaction.common/'objects/pack'/(view.pack_name+'.keep')
            old=keep.with_suffix('.retained')
            keep.rename(old)
            keep.write_bytes(old.read_bytes())
            replacement=keep.stat().st_ino
            with pytest.raises(ValueError,match='keep identity changed'):
                git_objects.release_keep(transaction)
            assert keep.stat().st_ino==replacement
            assert keep.read_bytes()==old.read_bytes()
    finally:transaction.cleanup()


@pytest.mark.parametrize('defect',['not-object','temporary-not-list','invalid-second-entry','identity-type'])
def test_corrupt_receipt_refuses_before_any_unlink(linked,defect):
    _,tree,reports,_=linked
    transaction=git_staging.Transaction.prepare(tree,reports)
    destination=transaction.common/'objects/pack'
    owned=destination/('.maestro-import-'+'a'*32)
    owned.write_text('retained temporary bytes')
    record=dict(pack='pack-'+'b'*40,journal=transaction.job,
        temporary=[dict(name=owned.name,identity=control_files.identity(owned.stat()))],keep=None)
    if defect=='not-object':record=[]
    elif defect=='temporary-not-list':record['temporary']='invalid'
    elif defect=='invalid-second-entry':record['temporary'].append(dict(name='../foreign',identity=[]))
    else:record['temporary'][0]['identity']='invalid'
    control_files.write(transaction.directory/'object-import.json',json.dumps(record),durable=True)
    try:
        with git_staging._guard(transaction.common/'maestro-native/object-publication'):
            with pytest.raises(ValueError,match='receipt'):
                git_objects.release_keep(transaction)
        assert owned.read_text()=='retained temporary bytes'
    finally:transaction.cleanup()


@pytest.mark.parametrize('seam',['initial-intent','temporary-intent','keep-fsync'])
def test_import_early_failure_refuses_ref_and_keeps_original_error(linked,monkeypatch,seam):
    _,tree,_,start=linked
    transaction=staged(linked)
    real_write=control_files.write
    real_sync=os.fsync
    reached=[]
    def write(path,text,**kwargs):
        if Path(path).name=='object-import.json':
            receipt=json.loads(text)
            match=(not receipt['temporary'] if seam=='initial-intent' else bool(receipt['temporary']))
            if seam!='keep-fsync' and match and not reached:
                reached.append(True)
                raise OSError('injected early import failure')
        return real_write(path,text,**kwargs)
    def sync(fd):
        if seam=='keep-fsync' and not reached:
            intent=transaction.directory/'object-import.json'
            if intent.exists() and json.loads(intent.read_text()).get('keep')==control_files.identity(os.fstat(fd)):
                reached.append(True)
                raise OSError('injected early import failure')
        return real_sync(fd)
    monkeypatch.setattr(control_files,'write',write)
    monkeypatch.setattr(os,'fsync',sync)
    try:
        with pytest.raises(OSError,match='injected early import failure'):
            transaction.finish(success=True)
        assert reached
        assert git(tree,'rev-parse','HEAD').stdout.strip()==start
        assert json.loads(Path(transaction.job).read_text())['state']=='aborted'
        destination=transaction.common/'objects/pack'
        assert not list(destination.glob('.maestro-import-*'))
        assert not list(destination.glob('*.keep'))
    finally:transaction.cleanup()
