#
# Copyright (c) 2006, 2007 Canonical
#
# Written by Gustavo Niemeyer <gustavo@niemeyer.net>
#
# This file is part of Storm Object Relational Mapper.
#
# Storm is free software; you can redistribute it and/or modify
# it under the terms of the GNU Lesser General Public License as
# published by the Free Software Foundation; either version 2.1 of
# the License, or (at your option) any later version.
#
# Storm is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
#
import traceback
import sys
import os

import transaction

from storm.zope.zstorm import ZStorm
from storm.locals import StormError

from tests.mocker import MockerTestCase

from storm.patch import Patch, PatchApplier, UnknownPatchError, BadPatchError


patch_test_0 = """
x = None
def apply(store):
    global x
    x = 42
    from mypackage import shared_data
    shared_data.append(42)
"""

patch_test_1 = """
y = None
def apply(store):
    global y
    y = 380
    from mypackage import shared_data
    shared_data.append(380)
"""

patch_explosion = """
from storm.locals import StormError

def apply(store):
    raise StormError('KABOOM!')
"""

patch_after_explosion = """
def apply(store):
    pass
"""

patch_no_args_apply = """
def apply():
    pass
"""

patch_missing_apply = """
def misnamed_apply(store):
    pass
"""

patch_name_error = """
def apply(store):
    blah # Comment
"""


class MockPatchStore(object):

    def __init__(self, database, patches=[]):
        self.database = database
        self.rolled_back = 0
        self.committed = 0
        self.patches = patches
        self.objs = []

    def execute(self, statement, params=None, noresult=False):
        pass

    def find(self, cls_spec, *args, **kwargs):
        return self.patches

    def rollback(self):
        self.rolled_back += 1

    def add(self, obj):
        self.objs.append(obj)

    def commit(self):
        self.committed += 1


class PatchTest(MockerTestCase):

    def add_module(self, module_filename, contents):
        filename = os.path.join(self.pkgdir, module_filename)
        file = open(filename, "w")
        file.write(contents)
        file.close()

    def remove_all_modules(self):
        for filename in os.listdir(self.pkgdir):
            os.unlink(os.path.join(self.pkgdir, filename))

    def setUp(self):
        super(PatchTest, self).setUp()

        self.zstorm = ZStorm()

        self.patchdir = self.makeDir()
        self.pkgdir = os.path.join(self.patchdir, "mypackage")
        os.makedirs(self.pkgdir)

        f = open(os.path.join(self.pkgdir, "__init__.py"), "w")
        f.write("shared_data = []")
        f.close()

        # Order of creation here is important to try to screw up the
        # patch ordering, as os.listdir returns in order of mtime (or
        # something).
        for pname, data in [("patch_380.py", patch_test_1),
                            ("patch_42.py", patch_test_0)]:
            self.add_module(pname, data)

        sys.path.append(self.patchdir)

        self.filename = self.makeFile()
        self.uri = "sqlite:///%s" % self.filename
        self.store = self.zstorm.create(None, self.uri)

        self.store.execute("CREATE TABLE patch "
                           "(version INTEGER NOT NULL PRIMARY KEY)")

        self.assertFalse(self.store.get(Patch, (42)))
        self.assertFalse(self.store.get(Patch, (380)))

        import mypackage
        self.mypackage = mypackage
        self.patch_applier = PatchApplier(self.store, self.mypackage)

        # Create another store just to keep track of the state of the
        # whole transaction manager.  See the assertion functions below.
        self.another_store = self.zstorm.create(None, "sqlite:")
        self.another_store.execute("CREATE TABLE test (id INT)")
        self.another_store.commit()
        self.prepare_for_transaction_check()

    def tearDown(self):
        super(PatchTest, self).tearDown()
        transaction.abort()
        self.zstorm._reset()
        sys.path.remove(self.patchdir)
        for name in list(sys.modules):
            if name == "mypackage" or name.startswith("mypackage."):
                del sys.modules[name]

    def prepare_for_transaction_check(self):
        self.another_store.execute("DELETE FROM test")
        self.another_store.execute("INSERT INTO test VALUES (1)")

    def assert_transaction_committed(self):
        self.another_store.rollback()
        result = self.another_store.execute("SELECT * FROM test").get_one()
        self.assertEquals(result, (1,),
                          "Transaction manager wasn't committed.")

    def assert_transaction_aborted(self):
        self.another_store.commit()
        result = self.another_store.execute("SELECT * FROM test").get_one()
        self.assertEquals(result, None,
                          "Transaction manager wasn't aborted.")

    def test_apply(self):
        self.patch_applier.apply(42)

        x = getattr(self.mypackage, "patch_42").x
        self.assertEquals(x, 42)
        self.assertTrue(self.store.get(Patch, (42)))
        self.assertTrue("mypackage.patch_42" in sys.modules)

        self.assert_transaction_committed()

    def test_apply_all(self):
        self.patch_applier.apply_all()

        self.assertTrue("mypackage.patch_42" in sys.modules)
        self.assertTrue("mypackage.patch_380" in sys.modules)

        x = getattr(self.mypackage, "patch_42").x
        y = getattr(self.mypackage, "patch_380").y

        self.assertEquals(x, 42)
        self.assertEquals(y, 380)

        self.assert_transaction_committed()

    def test_apply_exploding_patch(self):
        self.remove_all_modules()
        self.add_module("patch_666.py", patch_explosion)
        self.assertRaises(StormError, self.patch_applier.apply, 666)

        self.assert_transaction_aborted()

    def test_wb_apply_all_exploding_patch(self):
        """
        When a patch explodes the store is rolled back to make sure
        that any changes the patch made to the database are removed.
        Any other patches that have been applied successfully before
        it should not be rolled back.  Any patches pending after the
        exploding patch should remain unapplied.
        """
        self.add_module("patch_666.py", patch_explosion)
        self.add_module("patch_667.py", patch_after_explosion)
        self.assertEquals(list(self.patch_applier._get_unapplied_versions()),
                          [42, 380, 666, 667])
        self.assertRaises(StormError, self.patch_applier.apply_all)
        self.assertEquals(list(self.patch_applier._get_unapplied_versions()),
                          [666, 667])

    def test_mark_applied(self):
        self.patch_applier.mark_applied(42)

        self.assertFalse("mypackage.patch_42" in sys.modules)
        self.assertFalse("mypackage.patch_380" in sys.modules)

        self.assertTrue(self.store.get(Patch, 42))
        self.assertFalse(self.store.get(Patch, 380))

        self.assert_transaction_committed()

    def test_mark_applied_all(self):
        self.patch_applier.mark_applied_all()

        self.assertFalse("mypackage.patch_42" in sys.modules)
        self.assertFalse("mypackage.patch_380" in sys.modules)

        self.assertTrue(self.store.get(Patch, 42))
        self.assertTrue(self.store.get(Patch, 380))

        self.assert_transaction_committed()

    def test_application_order(self):
        self.patch_applier.apply_all()
        self.assertEquals(self.mypackage.shared_data,
                          [42, 380])

    def test_has_pending_patches(self):
        self.assertTrue(self.patch_applier.has_pending_patches())
        self.patch_applier.apply_all()
        self.assertFalse(self.patch_applier.has_pending_patches())

    def test_abort_if_unknown_patches(self):
        self.patch_applier.mark_applied(381)
        self.assertRaises(UnknownPatchError, self.patch_applier.apply_all)

    def test_get_unknown_patch_versions(self):
        patches = [Patch(42), Patch(380), Patch(381)]
        my_store = MockPatchStore("database", patches=patches)
        patch_applier = PatchApplier(my_store, self.mypackage)
        self.assertEqual(set([381]),
                         patch_applier.get_unknown_patch_versions())

    def test_no_unknown_patch_versions(self):
        patches = [Patch(42), Patch(380)]
        my_store = MockPatchStore("database", patches=patches)
        patch_applier = PatchApplier(my_store, self.mypackage)
        self.assertEqual(set(), patch_applier.get_unknown_patch_versions())

    def test_patch_with_incorrect_apply(self):
        self.add_module("patch_999.py", patch_no_args_apply)
        try:
            self.patch_applier.apply_all()
        except BadPatchError, e:
            self.assertTrue("mypackage/patch_999.py" in str(e))
            self.assertTrue("takes no arguments" in str(e))
            self.assertTrue("TypeError" in str(e))
        else:
            self.fail("BadPatchError not raised")

    def test_patch_with_missing_apply(self):
        self.add_module("patch_999.py", patch_missing_apply)
        try:
            self.patch_applier.apply_all()
        except BadPatchError, e:
            self.assertTrue("mypackage/patch_999.py" in str(e))
            self.assertTrue("no attribute" in str(e))
            self.assertTrue("AttributeError" in str(e))
        else:
            self.fail("BadPatchError not raised")

    def test_patch_with_syntax_error(self):
        self.add_module("patch_999.py", "that's not python")
        try:
            self.patch_applier.apply_all()
        except BadPatchError, e:
            self.assertTrue(" 999 " in str(e))
            self.assertTrue("SyntaxError" in str(e))
        else:
            self.fail("BadPatchError not raised")

    def test_patch_error_includes_traceback(self):
        self.add_module("patch_999.py", patch_name_error)
        try:
            self.patch_applier.apply_all()
        except BadPatchError, e:
            self.assertTrue("mypackage/patch_999.py" in str(e))
            self.assertTrue("NameError" in str(e))
            self.assertTrue("blah" in str(e))
            formatted = traceback.format_exc()
            self.assertTrue("# Comment" in formatted)
        else:
            self.fail("BadPatchError not raised")
