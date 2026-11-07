"""Retain complete vulnerability, secret and SBOM evidence for built game images."""
import json
import datetime
import hashlib
from pathlib import Path
import re
import stat
import subprocess
import tomllib
import dependencies

from configuration import relative
import images

REPORT_READER = dependencies.helper('image_security_report')
CONTEXT_READER = dependencies.helper('image_scan_context')


def declaration(root, config):
    value=config.get('components',{}).get('image_security')
    if not isinstance(value,dict) or set(value)!={'reports'}:
        raise ValueError('image security requires a generated report directory')
    directory=relative(root,value['reports'])
    if not directory.is_relative_to(root/'.artifacts'):
        raise ValueError('image evidence belongs to generated checkout artifacts')
    literal=(root/value['reports']).absolute()
    for parent in [*reversed(literal.parents),literal]:
        if parent.exists() or parent.is_symlink():
            info=parent.lstat()
            if not stat.S_ISDIR(info.st_mode) or getattr(info,'st_reparse_tag',0):
                raise ValueError('image evidence directories may not be linked or special')
    pin=tomllib.loads((root/'mise.toml').read_text()).get('env',{}).get('TRIVY_IMAGE','')
    if not re.fullmatch(r'aquasec/trivy@sha256:[a-f0-9]{64}',pin):
        raise ValueError('image scanner SDK requires its exact reviewed Mise digest')
    return directory,pin


def evidence_paths(root, config):
    directory,_=declaration(root,config)
    return [(directory/name/filename).relative_to(root).as_posix()
            for name in images.components(images.definition(root,config)) for filename in ['security.json','sbom.json']]


def setup(root, config, environment, run):
    _,scanner=declaration(root,config)
    run(['docker','pull',scanner],root,environment,timeout=300)


def scan(root, config, environment, run):
    definition=images.definition(root,config)
    records=list(zip(images.components(definition),images.references(definition,environment['SOURCE_COMMIT']),strict=True))
    return scan_references(root,config,environment,run,records)


def scan_references(root, config, environment, run, records):
    """Scan only literal source-family refs; historical authority is the caller's verified CD record.

    This creates scanner evidence only. It never issues a current-source build,
    product-test or aggregate acceptance receipt for historical images.
    """
    context_reader = CONTEXT_READER
    image_scan_findings = REPORT_READER.image_scan_findings
    MAX_ARCHIVE, object_pairs, regular = context_reader.MAX_ARCHIVE, context_reader.object_pairs, context_reader.regular
    directory,scanner=declaration(root,config)
    definition=images.definition(root,config)
    components=images.components(definition)
    current=dict(zip(components,images.references(definition,environment['SOURCE_COMMIT']),strict=True))
    if (not isinstance(records,(list,tuple)) or not 1<=len(records)<=len(components)
            or any(not isinstance(record,(list,tuple)) or len(record)!=2 for record in records)):
        raise ValueError('image scans require a bounded nonempty component/reference inventory')
    seen=set()
    for name,reference in records:
        if not isinstance(name,str) or name not in components or name in seen or not isinstance(reference,str):
            raise ValueError('image scans require distinct declared shipping components')
        seen.add(name)
        immutable=definition['image_base']+'-'+name+'@sha256:'
        if reference!=current[name] and not (reference.startswith(immutable) and re.fullmatch('[a-f0-9]{64}',reference[len(immutable):])):
            raise ValueError('image scans require this source tag or the exact immutable component family')
    def inspect(reference):
        values=json.loads(subprocess.check_output(['docker','image','inspect',reference],cwd=root,env=environment,timeout=60),object_pairs_hook=object_pairs)
        if not isinstance(values,list) or len(values)!=1 or not isinstance(values[0],dict):
            raise ValueError('image scanner requires one actual inspected image')
        return values[0]
    def image_identity(value):
        return {key:value.get(key) for key in ['Id','RootFS','Config','Os','Architecture']}
    def read(path,maximum=64<<20):
        _,identity=regular(path,maximum);body=path.read_bytes()
        if regular(path,maximum)[1]!=identity:
            raise ValueError('image scanner output changed during admission')
        return json.loads(body,object_pairs_hook=object_pairs),body,identity
    release_path=root/'.artifacts/release/release.json'
    accepted_images=None;release_sha=None
    if release_path.exists() or release_path.is_symlink():
        release,release_bytes,_=read(release_path,1<<20)
        accepted_images=release.get('images') if isinstance(release,dict) else None
        if (not isinstance(accepted_images,dict) or set(accepted_images)!=set(current.values())
                or release.get('revision')!=environment['SOURCE_COMMIT'] or release.get('project')!=definition['project']
                or any(not isinstance(value,str) or not re.fullmatch('sha256:[a-f0-9]{64}',value) for value in accepted_images.values())):
            raise ValueError('image security source inventory differs from the imported release identity map')
        release_sha=hashlib.sha256(release_bytes).hexdigest()
    scanned=inspect(scanner)
    scanner_identity={'reference':scanner,'id':scanned.get('Id'),'version':scanned.get('Config',{}).get('Labels',{}).get('org.opencontainers.image.version')}
    for name,reference in records:
        output=directory/name
        if output.exists() or output.is_symlink():
            info=output.lstat()
            if not stat.S_ISDIR(info.st_mode) or getattr(info,'st_reparse_tag',0):
                raise ValueError('image scan output directory must not be linked or special')
        output.mkdir(parents=True,exist_ok=True)
        # Refuse links before clearing prior evidence. A new scan must produce
        # both complete files, never inherit a successful output from an old run.
        for filename in ['security.json','sbom.json','scan-context.json','data-image.tar']:
            path=output/filename
            if path.exists() or path.is_symlink():
                regular(path,MAX_ARCHIVE if filename=='data-image.tar' else 64<<20)
                path.unlink()
        before_raw=inspect(reference);before=image_identity(before_raw)
        if reference==current[name] and accepted_images is not None and accepted_images[reference]!=before['Id']:
            raise ValueError('scanned image differs from its accepted source build identity')
        source=environment.get('EXPECTED_SOURCE_COMMIT',environment['SOURCE_COMMIT']) if '@sha256:' in reference else environment['SOURCE_COMMIT']
        started=datetime.datetime.now(datetime.timezone.utc).isoformat()
        base=['docker','run','--rm','--pull','never','-v','/var/run/docker.sock:/var/run/docker.sock:ro','-v',str(output)+':/reports',scanner,'image','--quiet','--skip-version-check','--image-src','docker']
        run([*base,'--scanners','vuln,secret','--format','json','--output','/reports/security.json',reference],root,environment)
        run([*base,'--format','cyclonedx','--output','/reports/sbom.json',reference],root,environment)
        after_raw=inspect(reference);after=image_identity(after_raw)
        context={'reference':reference,'source':source,'scanner':scanner_identity,'before':before,'after':after,
            'started_at':started,'finished_at':datetime.datetime.now(datetime.timezone.utc).isoformat()}
        report,report_bytes,report_identity=read(output/'security.json')
        sbom,sbom_bytes,sbom_identity=read(output/'sbom.json')
        archive=None;archive_identity=None;archive_sha=None;export_sizes=None
        if 'Results' not in report or report['Results']==[]:
            # Only an already scanned, small image can enter the exceptional
            # data-filesystem proof path; no source or image is built here.
            context_reader.validate(report,sbom,context)
            sizes=[before_raw.get('Size'),after_raw.get('Size')]
            if any(type(size) is not int or not 0<size<=16<<20 for size in sizes) or sizes[0]!=sizes[1]:
                raise ValueError('empty image results require consistent bounded actual inspected image sizes')
            # Trivy counts complete serialized layer streams (including tar
            # blocking); Docker inspect reports its own image-size domain.
            # Both must be bounded, never made equal by rewriting evidence.
            report_size=report['Metadata'].get('Size')
            if (type(report_size) is not int or not 0<report_size<=16<<20
                    or report_size!=sum(layer['Size'] for layer in report['Metadata']['Layers'])):
                raise ValueError('empty image report size differs from its bounded scanned layer streams')
            export_sizes={'docker_inspect_bytes':sizes[0],'trivy_layer_stream_bytes':report_size}
            archive=output/'data-image.tar'
            exporting=inspect(reference)
            if (image_identity(exporting)!=before or type(exporting.get('Size')) is not int or exporting['Size']!=sizes[0]
                    or (exporting.get('Config',{}).get('Labels') or {}).get('org.opencontainers.image.revision')!=source):
                raise ValueError('data image source, tag identity or actual size changed before export')
            run(['docker','image','save','--output',str(archive),reference],root,environment,timeout=60)
            archive_identity=regular(archive,MAX_ARCHIVE)[1]
            archive_sha=hashlib.sha256(archive.read_bytes()).hexdigest()
        blocked=image_scan_findings(report,sbom,context=context,data_archive=archive)
        for path,body,wanted in [(output/'security.json',report_bytes,report_identity),(output/'sbom.json',sbom_bytes,sbom_identity)]:
            if regular(path,64<<20)[1]!=wanted or path.read_bytes()!=body:
                raise ValueError('image scanner output changed after admission')
        if archive and (regular(archive,MAX_ARCHIVE)[1]!=archive_identity or hashlib.sha256(archive.read_bytes()).hexdigest()!=archive_sha):
            raise ValueError('data image evidence changed after admission')
        if release_sha is not None and hashlib.sha256(release_path.read_bytes()).hexdigest()!=release_sha:
            raise ValueError('imported release identity map changed during image security checks')
        record=json.dumps({'version':1,'context':context,'accepted_release_sha256':release_sha,'data_export_sizes':export_sizes,
            'result_shape':'omitted-data' if 'Results' not in report else ('empty-data' if report['Results']==[] else 'results'),
            'security_sha256':hashlib.sha256(report_bytes).hexdigest(),
            'sbom_sha256':hashlib.sha256(sbom_bytes).hexdigest(),
            'data_archive_sha256':archive_sha},sort_keys=True)+'\n'
        with (output/'scan-context.json').open('x') as target:target.write(record)
        if blocked:raise ValueError('release image has High/Critical vulnerabilities or detected secrets; full report retained')
