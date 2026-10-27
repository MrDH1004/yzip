"""assoc on a throwaway extension (.yzt): the user's real .zip/.7z handlers are never touched."""
import os
import unittest

import assoc

EXT = ".yzt"
EXE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dist", "yzip", "yzip.exe")


class AssocTests(unittest.TestCase):
    def setUp(self):
        if not os.path.exists(EXE):
            self.skipTest("build the exe first")
        assoc.TYPES[EXT] = "YZT 테스트 파일"

    def tearDown(self):
        assoc.unregister([EXT])
        assoc._del_tree(rf"Software\Classes\{EXT}")
        assoc._del_tree(r"Software\Classes\Other.yzt")
        assoc.TYPES.pop(EXT, None)

    def test_register_makes_windows_open_it_with_yzip(self):
        self.assertFalse(assoc.status([EXT], EXE)[EXT][0])
        self.assertFalse(assoc.enabled([EXT]))
        assoc.register([EXT], exe=EXE)
        mine, name = assoc.status([EXT], EXE)[EXT]
        self.assertTrue(mine, name)
        self.assertTrue(assoc.enabled([EXT]))
        self.assertEqual(assoc._get(rf"Software\Classes\{EXT}\OpenWithProgids", "yzip" + EXT), "")
        assoc.unregister([EXT])
        self.assertFalse(assoc.status([EXT], EXE)[EXT][0])
        self.assertFalse(assoc.enabled([EXT]))
        self.assertIsNone(assoc._get(r"Software\Classes\yzip" + EXT))

    def test_previous_default_is_restored(self):
        assoc._set(r"Software\Classes\Other.yzt\shell\open\command", "", r'"C:\Windows\notepad.exe" "%1"')
        assoc._set(rf"Software\Classes\{EXT}", "", "Other.yzt")
        assoc.register([EXT], exe=EXE)
        self.assertTrue(assoc.status([EXT], EXE)[EXT][0])
        assoc.unregister([EXT])
        self.assertEqual(assoc._get(rf"Software\Classes\{EXT}"), "Other.yzt")
        self.assertIn("notepad", assoc.effective(EXT)[0].lower())


if __name__ == "__main__":
    unittest.main()
