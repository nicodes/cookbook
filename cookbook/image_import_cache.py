"""Recipe-facing access to the separately verified generic archive core."""
import dependencies


def remaining(deadline):
    return dependencies.helper('image_import_cache').remaining(deadline)


def archive_images(path, expected, deadline):
    return dependencies.helper('image_import_cache').archive_images(path, expected, deadline)


def reload(root, source, archives, expected, environment, timeout=600):
    return dependencies.helper('image_import_cache').reload(root, source, archives, expected, environment, timeout)
