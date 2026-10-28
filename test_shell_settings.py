"""Shell preference registration without touching the user's registry."""
import os
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import yzip_gui as gui


class ShellSettingsTests(unittest.TestCase):
    def test_disabled_shell_unregisters_instead_of_registering(self):
        with mock.patch.object(gui, "load_settings", return_value={"shell_enabled": False}), \
                mock.patch.object(gui, "shell_unregister") as unregister:
            gui.shell_register()
        unregister.assert_called_once_with()

    def test_menu_mask_and_language_are_written_for_native_handler(self):
        settings = {"shell_enabled": True, "shell_compress": False,
                    "shell_extract_here": True, "shell_extract_folder": False, "language": "en"}
        writes = []
        key = mock.MagicMock()
        with mock.patch.object(gui, "load_settings", return_value=settings), \
                mock.patch.object(gui, "_delete_key_tree"), mock.patch.object(gui, "_delete_tree"), \
                mock.patch.object(gui, "shell_dll_source", return_value=""), \
                mock.patch("winreg.CreateKey", return_value=key), \
                mock.patch("winreg.SetValueEx", side_effect=lambda *args: writes.append(args)):
            gui.shell_register()
        self.assertTrue(any(args[1] == "MenuMask" and args[4] == 2 for args in writes))
        self.assertTrue(any(args[1] == "Language" and args[4] == "en" for args in writes))
        commands = [args[4] for args in writes if args[1] == "" and isinstance(args[4], str)]
        self.assertTrue(any("--extract-here" in command for command in commands))
        self.assertFalse(any("--archive" in command or "--extract-folder" in command for command in commands))


if __name__ == "__main__":
    unittest.main()
