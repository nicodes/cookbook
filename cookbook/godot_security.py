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
import tempfile
import artifact_packet
import deployment

from configuration import relative
import images
import native_runtime

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
    if not re.fullmatch(r'docker.io/aquasec/trivy@sha256:[a-f0-9]{64}',pin):
        raise ValueError('image scanner SDK requires its exact reviewed Mise digest')
    return directory,pin


def components(config, definition):
    selected = images.components(definition)
    if any(record['name'] == 'gate' for record in config.get('components', {}).get('images', [])):
        selected += ['gate']
    return selected


def evidence_paths(root, config):
    directory,_=declaration(root,config)
    paths = [(directory/name/filename).relative_to(root).as_posix()
             for name in components(config,images.definition(root,config)) for filename in ['security.json','sbom.json']]
    paths += [(directory/'assets'/name/filename).relative_to(root).as_posix()
              for name in (deployment.assets(root,config) if config.get('components',{}).get('assets') else {})
              for filename in ['security.json','sbom.json','scan-context.json']]
    return paths


def setup(root, config, environment, run):
    _,scanner=declaration(root,config)
    native_runtime.execute(['pull',scanner],root,environment,run,timeout=300)


def scan(root, config, environment, run):
    definition=images.definition(root,config)
    records=[(name,definition['image_base']+'-'+name+':'+environment['SOURCE_COMMIT']) for name in components(config,definition)]
    if records:
        scan_references(root,config,environment,run,records)
    scan_assets(root,config,environment,run)


def scan_assets(root, config, environment, run):
    """Scan the exact public archive bytes without constructing an app image."""
    if not config.get('components',{}).get('assets'):
        return
    records=deployment.assets(root,config)
    release=deployment.verify(root,environment)
    release_path=Path(root)/'.artifacts/release/release.json'
    release_sha=artifact_packet.digest_file(release_path)[0]
    directory,scanner=declaration(root,config)
    inspected=native_runtime.inspect([scanner],environment)[0]
    scanner_identity={'reference':scanner,'id':inspected['Id'],
        'version':(inspected.get('Config',{}).get('Labels') or {}).get('org.opencontainers.image.version')}
    for name,record in records.items():
        output=relative(root,(directory/'assets'/name).relative_to(root).as_posix())
        output.mkdir(parents=True,exist_ok=True)
        for filename in ['security.json','sbom.json','scan-context.json']:
            path=output/filename
            if path.exists() or path.is_symlink():
                artifact_packet.regular(path,64<<20)
                path.unlink()
        archive=Path(root)/'.artifacts/release/static'/(name+'.tar.gz')
        artifact=release['artifacts'][name]
        limits={'max_bytes':record['max_bytes'],'max_file_bytes':record['max_file_bytes'],
            'max_files':record.get('max_files',artifact_packet.MAX_MEMBERS)}
        with tempfile.TemporaryDirectory(prefix='.static-',dir=output) as scratch:
            source=Path(scratch)/'public'
            artifact_packet.unpack_static(archive,source,**limits)
            def inventory():
                return {path:{'sha256':artifact_packet.digest_file(source/path)[0],'size':info.st_size,
                    'mode':info.st_mode&0o777} for path,info in artifact_packet.static_sources(source,**limits).items()}
            before=inventory()
            started=datetime.datetime.now(datetime.timezone.utc).isoformat()
            base=['run','--rm','--pull=never','--cap-drop=ALL','--security-opt=no-new-privileges',
                '-v',str(source)+':/inputs/static:ro','-v',str(output)+':/reports',scanner,
                'fs','--quiet','--skip-version-check']
            native_runtime.execute([*base,'--scanners','vuln,secret','--format','json','--output','/reports/security.json','/inputs/static'],root,environment,run)
            native_runtime.execute([*base,'--format','cyclonedx','--output','/reports/sbom.json','/inputs/static'],root,environment,run)
            context={'source':release['source']['revision'],'release_digest':'sha256:'+release_sha,
                'artifact':artifact,'target':'/inputs/static','scanner':scanner_identity,
                'started_at':started,'finished_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),
                'before':before,'after':inventory()}
            report=artifact_packet.read_json(output/'security.json',64<<20)
            sbom=artifact_packet.read_json(output/'sbom.json',64<<20)
            blocked=REPORT_READER.filesystem_scan_findings(report,sbom,context=context,input_archive=archive)
        if artifact_packet.digest_file(release_path)[0]!=release_sha:
            raise ValueError('accepted static release changed during filesystem scanning')
        evidence={'version':1,'context':context,
            'security_sha256':artifact_packet.digest_file(output/'security.json')[0],
            'sbom_sha256':artifact_packet.digest_file(output/'sbom.json')[0]}
        with (output/'scan-context.json').open('x') as target:
            target.write(json.dumps(evidence,sort_keys=True)+'\n')
        if blocked:
            raise ValueError('release static archive has High/Critical vulnerabilities or detected secrets; full report retained')


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
    selected_components=components(config,definition)
    current={name:definition['image_base']+'-'+name+':'+environment['SOURCE_COMMIT'] for name in selected_components}
    if (not isinstance(records,(list,tuple)) or not 1<=len(records)<=len(selected_components)
            or any(not isinstance(record,(list,tuple)) or len(record)!=2 for record in records)):
        raise ValueError('image scans require a bounded nonempty component/reference inventory')
    seen=set()
    for name,reference in records:
        if not isinstance(name,str) or name not in selected_components or name in seen or not isinstance(reference,str):
            raise ValueError('image scans require distinct declared shipping components')
        seen.add(name)
        immutable=definition['image_base']+'-'+name+'@sha256:'
        if reference!=current[name] and not (reference.startswith(immutable) and re.fullmatch('[a-f0-9]{64}',reference[len(immutable):])):
            raise ValueError('image scans require this source tag or the exact immutable component family')
    def inspect(reference):
        values=native_runtime.inspect([reference],environment)
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
        import image_fixtures
        fixtures=image_fixtures.verify(root,config,environment)
        accepted_images={reference:release.get('artifacts',{}).get(name,{}).get('config_digest',fixtures.get(reference))
                         for name,reference in current.items()}
        if (release.get('kind')!='komizo.release' or release.get('source',{}).get('revision')!=environment['SOURCE_COMMIT']
                or release.get('application')!=definition['project']
                or any(not isinstance(value,str) or not re.fullmatch('sha256:[a-f0-9]{64}',value) for value in accepted_images.values())):
            raise ValueError('image security inventory differs from the imported release descriptor')
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
        archive=output/'data-image.tar'
        dependencies.helper('oci_archive').save_image(reference,archive,environment=environment)
        with tempfile.TemporaryDirectory(prefix='.oci-',dir=output) as scratch:
            layout=Path(scratch)/'image.oci'
            with dependencies.helper('oci_archive').OCIArchive(archive) as accepted:
                admitted_archive_sha=accepted.metadata['archive_sha256']
                accepted.materialize(layout)
                base=['run','--rm','--pull=never','-v',str(layout)+':/inputs/image.oci:ro',
                    '-v',str(output)+':/reports',scanner,'image','--quiet','--skip-version-check','--input','/inputs/image.oci']
                native_runtime.execute([*base,'--scanners','vuln,secret','--format','json','--output','/reports/security.json'],root,environment,run)
                native_runtime.execute([*base,'--format','cyclonedx','--output','/reports/sbom.json'],root,environment,run)
                accepted.unchanged()
        after_raw=inspect(reference);after=image_identity(after_raw)
        context={'archive':{'sha256':admitted_archive_sha,'target':'/inputs/image.oci'},'reference':reference,'source':source,'scanner':scanner_identity,'before':before,'after':after,
            'started_at':started,'finished_at':datetime.datetime.now(datetime.timezone.utc).isoformat()}
        context_reader.authenticate_archive(archive,context,before)
        report,report_bytes,report_identity=read(output/'security.json')
        sbom,sbom_bytes,sbom_identity=read(output/'sbom.json')
        archive_identity=None;archive_sha=None;export_sizes=None
        input_archive=archive
        archive=None
        if 'Results' not in report or report['Results']==[]:
            # Only an already scanned, small image can enter the exceptional
            # data-filesystem proof path; no source or image is built here.
            context_reader.validate(report,sbom,context)
            sizes=[before_raw.get('Size'),after_raw.get('Size')]
            if any(type(size) is not int or not 0<size<=16<<20 for size in sizes) or sizes[0]!=sizes[1]:
                raise ValueError('empty image results require consistent bounded actual inspected image sizes')
            # Trivy counts complete serialized layer streams (including tar
            # blocking); Podman inspect reports its own image-size domain.
            # Both must be bounded, never made equal by rewriting evidence.
            report_size=report['Metadata'].get('Size')
            if (type(report_size) is not int or not 0<report_size<=16<<20
                    or report_size!=sum(layer['Size'] for layer in report['Metadata']['Layers'])):
                raise ValueError('empty image report size differs from its bounded scanned layer streams')
            export_sizes={'podman_inspect_bytes':sizes[0],'trivy_layer_stream_bytes':report_size}
            archive=output/'data-image.tar'
            exporting=inspect(reference)
            if (image_identity(exporting)!=before or type(exporting.get('Size')) is not int or exporting['Size']!=sizes[0]
                    or (exporting.get('Config',{}).get('Labels') or {}).get('org.opencontainers.image.revision')!=source):
                raise ValueError('data image source, tag identity or actual size changed before export')
            dependencies.helper('oci_archive').inspect_archive(archive,expected_config=before['Id'])
            archive_identity=regular(archive,MAX_ARCHIVE)[1]
            archive_sha=hashlib.sha256(archive.read_bytes()).hexdigest()
        blocked=image_scan_findings(report,sbom,context=context,input_archive=input_archive,data_archive=archive)
        if archive is None:input_archive.unlink()
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
