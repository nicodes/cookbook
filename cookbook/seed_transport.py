"""Byte-exact public bounded seed transport shared with immutable Tools."""
import base64
import hashlib
import re
import zlib

SEED_LOADER = (
    'import base64,hashlib,sys,zlib;'
    '_sdk_decoder=zlib.decompressobj();'
    '_sdk_seed=_sdk_decoder.decompress(base64.b64decode("{encoded}",validate=True),{limit});'
    '(_sdk_decoder.eof and not _sdk_decoder.unused_data and not _sdk_decoder.unconsumed_tail '
    'and len(_sdk_seed)=={length} and hashlib.sha256(_sdk_seed).hexdigest()=="{sha256}") '
    'or sys.exit("SDK bootstrap seed failed integrity validation");'
    'exec(compile(_sdk_seed,"<immutable SDK bootstrap>","exec"))'
)


def seed_transport(seed, *, encoded, length, sha256):
    """Render the one fixed, bounded transport; never compress source at runtime."""
    if (not isinstance(seed, str) or type(length) is not int or not 0 < length <= 65536
            or not isinstance(encoded, str) or len(encoded) > 65536
            or not isinstance(sha256, str) or not re.fullmatch('[a-f0-9]{64}', sha256)):
        raise ValueError('SDK bootstrap requires one reviewed bounded seed transport')
    try:
        decoder = zlib.decompressobj()
        raw = decoder.decompress(base64.b64decode(encoded, validate=True), length + 1)
    except (ValueError, zlib.error) as error:
        raise ValueError('SDK bootstrap seed failed integrity validation') from error
    if (not decoder.eof or decoder.unused_data or decoder.unconsumed_tail
            or len(raw) != length or hashlib.sha256(raw).hexdigest() != sha256
            or raw != seed.encode('utf-8')):
        raise ValueError('SDK bootstrap seed failed integrity validation')
    source = SEED_LOADER.format(encoded=encoded, limit=length + 1, length=length, sha256=sha256)
    if '\n' in source or '\r' in source or len(source.encode('ascii')) > 7000:
        raise ValueError('SDK bootstrap transport must remain one bounded native command line')
    return source
