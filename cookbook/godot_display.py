"""One display profile for every Godot game: a 1080p design canvas in portrait or landscape,
stretched by canvas items with the expand aspect, and a desktop window that fits a 1080p monitor
with its title bar and taskbar. Phones run fullscreen, so the window only sets desktop launches."""
from pathlib import Path

PROFILES = {
    'portrait': {'viewport': (1080, 1920), 'window': (540, 960), 'orientations': {'1', '5'}},
    'landscape': {'viewport': (1920, 1080), 'window': (1280, 720), 'orientations': {'0', '4'}},
}
MAX_PROJECT_BYTES = 1 << 20


def settings(text):
    """The [display] section of a project.godot, unquoted."""
    section, values = None, {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(';'):
            continue
        if line.startswith('[') and line.endswith(']'):
            section = line[1:-1]
        elif section == 'display' and '=' in line:
            key, value = line.split('=', 1)
            values[key.strip()] = value.strip().strip('"')
    return values


def _size(values, width, height):
    raw = (values.get('window/size/' + width), values.get('window/size/' + height))
    if None in raw:
        return None
    if not all(value.isdigit() for value in raw):
        raise ValueError('Godot display sizes must be whole numbers')
    return tuple(map(int, raw))


def validate(directory):
    """Returns the game's profile name, or refuses a project that drifts from it."""
    path = Path(directory) / 'project.godot'
    if path.stat().st_size > MAX_PROJECT_BYTES:
        raise ValueError('project.godot exceeds its reviewed size')
    values = settings(path.read_text(encoding='utf-8'))
    viewport = _size(values, 'viewport_width', 'viewport_height')
    name = next((name for name, profile in PROFILES.items() if profile['viewport'] == viewport), None)
    if name is None:
        raise ValueError('Godot games design on the 1080x1920 portrait or 1920x1080 landscape canvas')
    profile = PROFILES[name]
    if values.get('window/stretch/mode') != 'canvas_items' or values.get('window/stretch/aspect') != 'expand':
        raise ValueError('Godot games stretch canvas items with the expand aspect')
    if values.get('window/stretch/scale_mode', 'fractional') != 'fractional':
        raise ValueError('Godot games scale their canvas fractionally')
    if _size(values, 'window_width_override', 'window_height_override') != profile['window']:
        width, height = profile['window']
        raise ValueError(f'a {name} Godot game opens a {width}x{height} desktop window')
    if values.get('window/handheld/orientation', '0') not in profile['orientations']:
        raise ValueError(f'a {name} Godot game keeps a {name} handheld orientation')
    return name
