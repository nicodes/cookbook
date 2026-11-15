from source_fixture import SDK_ROOT
"""Every Godot game keeps one display profile: a 1080p canvas and a window that fits 1080p."""
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(SDK_ROOT / 'cookbook'))
import godot_display as display

PORTRAIT = '''config_version=5

[application]

config/name="Game"

[display]

window/size/viewport_width=1080
window/size/viewport_height=1920
window/size/window_width_override=540
window/size/window_height_override=960
window/stretch/mode="canvas_items"
window/stretch/aspect="expand"
window/handheld/orientation=1

[rendering]

textures/canvas_textures/default_texture_filter=1
'''

LANDSCAPE = '''[display]
; Godot's default handheld orientation is landscape.
window/size/viewport_width=1920
window/size/viewport_height=1080
window/size/window_width_override=1280
window/size/window_height_override=720
window/stretch/mode="canvas_items"
window/stretch/aspect="expand"
'''


class GodotDisplay(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def check(self, text):
        (self.root / 'project.godot').write_text(text)
        return display.validate(self.root)

    def test_both_profiles_are_accepted(self):
        self.assertEqual(self.check(PORTRAIT), 'portrait')
        self.assertEqual(self.check(LANDSCAPE), 'landscape')
        self.assertEqual(self.check(LANDSCAPE + 'window/handheld/orientation=4\n'), 'landscape')
        self.assertEqual(self.check(PORTRAIT.replace('orientation=1', 'orientation=5')), 'portrait')

    def test_other_canvases_are_refused(self):
        for width, height in [('1280', '720'), ('360', '800'), ('1080', '1080')]:
            with self.subTest(width=width, height=height):
                text = LANDSCAPE.replace('viewport_width=1920', 'viewport_width=' + width)
                text = text.replace('viewport_height=1080', 'viewport_height=' + height)
                with self.assertRaisesRegex(ValueError, 'design on the 1080x1920'):
                    self.check(text)
        with self.assertRaisesRegex(ValueError, 'design on the 1080x1920'):
            self.check('config_version=5\n')
        with self.assertRaisesRegex(ValueError, 'design on the 1080x1920'):
            self.check(PORTRAIT.replace('[display]', '[displays]'))

    def test_stretching_is_fixed(self):
        for old, new in [('mode="canvas_items"', 'mode="viewport"'), ('aspect="expand"', 'aspect="keep"'),
                ('window/stretch/mode="canvas_items"\n', '')]:
            with self.subTest(old=old):
                with self.assertRaisesRegex(ValueError, 'canvas items with the expand'):
                    self.check(PORTRAIT.replace(old, new))
        with self.assertRaisesRegex(ValueError, 'fractionally'):
            self.check(LANDSCAPE + 'window/stretch/scale_mode="integer"\n')
        self.assertEqual(self.check(LANDSCAPE + 'window/stretch/scale_mode="fractional"\n'), 'landscape')

    def test_the_window_fits_a_1080p_monitor(self):
        for old, new in [('height_override=960', 'height_override=1170'),
                ('width_override=540\n', ''), ('height_override=960', 'height_override=961')]:
            with self.subTest(old=old):
                with self.assertRaisesRegex(ValueError, 'portrait Godot game opens a 540x960'):
                    self.check(PORTRAIT.replace(old, new))
        with self.assertRaisesRegex(ValueError, 'landscape Godot game opens a 1280x720'):
            self.check(LANDSCAPE.replace('width_override=1280', 'width_override=1560'))
        with self.assertRaisesRegex(ValueError, 'whole numbers'):
            self.check(LANDSCAPE.replace('width_override=1280', 'width_override=12.5'))

    def test_handhelds_keep_the_profile_orientation(self):
        with self.assertRaisesRegex(ValueError, 'portrait handheld orientation'):
            self.check(PORTRAIT.replace('orientation=1', 'orientation=4'))
        with self.assertRaisesRegex(ValueError, 'portrait handheld orientation'):
            self.check(PORTRAIT.replace('window/handheld/orientation=1\n', ''))
        with self.assertRaisesRegex(ValueError, 'landscape handheld orientation'):
            self.check(LANDSCAPE + 'window/handheld/orientation=1\n')

    def test_oversized_projects_are_refused(self):
        with self.assertRaisesRegex(ValueError, 'reviewed size'):
            self.check(PORTRAIT + ';' * display.MAX_PROJECT_BYTES)


if __name__ == '__main__':
    unittest.main()
