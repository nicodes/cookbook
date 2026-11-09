"""Use the selected generic native engine without remote sockets or aliases."""
import subprocess
import dependencies


def environment(values=None):
    return dependencies.helper('container_runtime').environment(values)


def command(arguments, environment=None):
    return dependencies.helper('container_runtime').command(arguments, environment)


def run(arguments, *, environment=None, **kwargs):
    return dependencies.helper('container_runtime').run(arguments, environment=environment, **kwargs)


def capture(arguments, *, environment=None, **kwargs):
    return run(arguments, environment=environment, check=True, stdout=subprocess.PIPE, **kwargs).stdout


def execute(arguments, root, environment, callback, **kwargs):
    argv, values = command(arguments, environment)
    return callback(argv, root, values, **kwargs)


def inspect(references, environment=None):
    return dependencies.helper('container_runtime').inspect_images(references, environment=environment)


def bind_mount_user_namespace(uid=None, gid=None):
    return dependencies.helper('container_runtime').bind_mount_user_namespace(uid, gid)
