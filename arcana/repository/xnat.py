import os
import os.path as op
import tempfile
import stat
import time
import logging
import errno
import json
import re
from zipfile import ZipFile, BadZipfile
import shutil
from tqdm import tqdm
import xnat
from arcana.utils import JSON_ENCODING
from arcana.utils import makedirs
from arcana.data import Fileset, Field
from arcana.repository.base import Repository
from arcana.exceptions import (
    ArcanaError, ArcanaUsageError, ArcanaFileFormatError,
    ArcanaWrongRepositoryError)
from arcana.pipeline.provenance import Record
from arcana.utils import dir_modtime, get_class_info, parse_value
from .dataset import Dataset


logger = logging.getLogger('arcana')

special_char_re = re.compile(r'[^a-zA-Z_0-9]')
tag_parse_re = re.compile(r'\((\d+),(\d+)\)')

RELEVANT_DICOM_TAG_TYPES = set(('UI', 'CS', 'DA', 'TM', 'SH', 'LO',
                                'PN', 'ST', 'AS'))


class XnatDataset(Dataset):
    """
    A representation of an XNAT "dataset", the complete collection of data
    (file-sets and fields) to be used in an analysis.

    Parameters
    ----------
    name : str
        The name/id/path that uniquely identifies the datset within the
        repository it is stored
    repository : Repository
        The repository the dataset belongs to
    subject_label_format : str
        A string used to generate the subject label from project and
        subject IDs, e.g. "{project}_{subject}"
    session_label_format : str
        A string used to generate the session label from project and
        subject and visit IDs, e.g. "{project}_{subject}_{visit}"
    subject_ids : list[str]
        Subject IDs to be included in the analysis. All other subjects are
        ignored
    visit_ids : list[str]
        Visit IDs to be included in the analysis. All other visits are ignored
    fill_tree : bool
        Whether to fill the tree of the destination repository with the
        provided subject and/or visit IDs. Intended to be used when the
        destination repository doesn't contain any of the the input
        filesets/fields (which are stored in external repositories) and
        so the sessions will need to be created in the destination
        repository.
    depth : int (0|1|2)
        The depth of the dataset (i.e. whether it has subjects and sessions).
            0 -> single session
            1 -> multiple subjects
            2 -> multiple subjects and visits
    subject_id_map : dict[str, str]
        Maps subject IDs in dataset to a global name-space
    visit_id_map : dict[str, str]
        Maps visit IDs in dataset to a global name-space
    """

    def __init__(self, name, repository, subject_label_format="{subject}",
                 session_label_format="{subject}_{visit}", **kwargs):
        super().__init__(name, repository, **kwargs)
        self.subject_label_format = subject_label_format
        self.session_label_format = session_label_format

    def subject_label(self, subject_id):
        return self.subject_label_format.format(
            project=self.name,
            subject=self.inv_map_subject_id(subject_id))


    def session_label(self, subject_id, visit_id):
        return self.session_label_format.format(
            project=self.name,
            subject=self.inv_map_subject_id(subject_id),
            visit=self.inv_map_visit_id(visit_id))

class XnatRepo(Repository):
    """
    A 'Repository' class for XNAT repositories

    Parameters
    ----------
    server : str (URI)
        URI of XNAT server to connect to
    project_id : str
        The ID of the project in the XNAT repository
    cache_dir : str (path)
        Path to local directory to cache remote data in
    user : str
        Username with which to connect to XNAT with
    password : str
        Password to connect to the XNAT repository with
    check_md5 : bool
        Whether to check the MD5 digest of cached files before using. This
        checks for updates on the server since the file was cached
    race_cond_delay : int
        The amount of time to wait before checking that the required
        fileset has been downloaded to cache by another process has
        completed if they are attempting to download the same fileset
    session_filter : str
        A regular expression that is used to prefilter the discovered sessions
        to avoid having to retrieve metadata for them, and potentially speeding
        up the initialisation of the Analysis. Note that if the processing
        relies on summary derivatives (i.e. of 'per_visit/subject/analysis'
        frequency) then the filter should match all sessions in the Analysis's
        subject_ids and visit_ids.
    """

    type = 'xnat'

    MD5_SUFFIX = '.__md5__.json'
    PROV_RESOURCE = 'PROV__'
    depth = 2

    def __init__(self, server, cache_dir, user=None,
                 password=None, check_md5=True, race_cond_delay=30,
                 session_filter=None):
        super().__init__()
        if not isinstance(server, str):
            raise ArcanaUsageError(
                "Invalid server url {}".format(server))
        self._server = server
        self.cache_dir = cache_dir
        makedirs(self.cache_dir, exist_ok=True)
        self.user = user
        self.password = password
        self._race_cond_delay = race_cond_delay
        self.check_md5 = check_md5
        self.session_filter = session_filter
        self.login = None

    def __hash__(self):
        return (hash(self.server)
                ^ hash(self.cache_dir)
                ^ hash(self._race_cond_delay)
                ^ hash(self.check_md5))

    def __repr__(self):
        return ("{}(server={}, cache_dir={})"
                .format(type(self).__name__,
                        self.server, self.cache_dir))

    def __eq__(self, other):
        try:
            return (self.server == other.server
                    and self.cache_dir == other.cache_dir
                    and self.cache_dir == other.cache_dir
                    and self._race_cond_delay == other._race_cond_delay
                    and self.check_md5 == other.check_md5)
        except AttributeError:
            return False  # For comparison with other types

    def __getstate__(self):
        dct = self.__dict__.copy()
        del dct['_login']
        del dct['_connection_depth']
        return dct

    def __setstate__(self, state):
        self.__dict__.update(state)
        self.login = None
        self._connection_depth = 0

    @property
    def prov(self):
        return {
            'type': get_class_info(type(self)),
            'server': self.server}

    @property
    def login(self):
        if self.login is None:
            raise ArcanaError("XNAT repository has been disconnected before "
                              "exiting outer context")
        return self.login

    @property
    def server(self):
        return self._server

    def dataset_cache_dir(self, dataset_name):
        return op.join(self.cache_dir, dataset_name)

    def connect(self):
        """
        Parameters
        ----------
        prev_login : xnat.XNATSession
            An XNAT login that has been opened in the code that calls
            the method that calls login. It is wrapped in a
            NoExitWrapper so the returned connection can be used
            in a "with" statement in the method.
        """
        sess_kwargs = {}
        if self.user is not None:
            sess_kwargs['user'] = self.user
        if self.password is not None:
            sess_kwargs['password'] = self.password
        self.login = xnat.connect(server=self._server, **sess_kwargs)

    def disconnect(self):
        self.login.disconnect()
        self.login = None

    def dataset(self, name, **kwargs):
        """
        Returns a dataset from the XNAT repository

        Parameters
        ----------
        name : str
            The name, path or ID of the dataset within the repository
        subject_label_format : str
            A string used to generate the subject label from project and
            subject IDs, e.g. "{project}_{subject}"
        session_label_format : str
            A string used to generate the session label from project and
            subject and visit IDs, e.g. "{project}_{subject}_{visit}"
        subject_ids : list[str]
            Subject IDs to be included in the analysis. All other subjects are
            ignored
        visit_ids : list[str]
            Visit IDs to be included in the analysis. All other visits are ignored
        fill_tree : bool
            Whether to fill the tree of the destination repository with the
            provided subject and/or visit IDs. Intended to be used when the
            destination repository doesn't contain any of the the input
            filesets/fields (which are stored in external repositories) and
            so the sessions will need to be created in the destination
            repository.
        subject_id_map : dict[str, str]
            Maps subject IDs in dataset to a global name-space
        visit_id_map : dict[str, str]
            Maps visit IDs in dataset to a global name-space
        """
        return XnatDataset(name, repository=self, depth=2, **kwargs)

    def get_fileset(self, fileset):
        """
        Caches a single fileset (if the 'path' attribute is accessed
        and it has not been previously cached for example

        Parameters
        ----------
        fileset : Fileset
            The fileset to cache
        prev_login : xnat.XNATSession
            An XNATSession object to use for the connection. A new
            one is created if one isn't provided

        Returns
        -------
        primary_path : str
            The path of the primary file once it has been cached
        aux_paths : dict[str, str]
            A dictionary containing a mapping of auxiliary file names to
            paths
        """
        if fileset.format is None:
            raise ArcanaUsageError(
                "Attempting to download {}, which has not been assigned a "
                "file format (see Fileset.formatted)".format(fileset))
        self._check_repository(fileset)
        with self:  # Connect to the XNAT repository if haven't already
            xsession = self.get_xsession(fileset)
            xscan = xsession.scans[fileset.name]
            # Set URI so we can retrieve checksums if required
            fileset.uri = xscan.uri
            fileset.id = xscan.id
            cache_path = self._cache_path(fileset)
            need_to_download = True
            if op.exists(cache_path):
                if self.check_md5:
                    md5_path = cache_path + XnatRepo.MD5_SUFFIX
                    try:
                        with open(md5_path, 'r') as f:
                            cached_checksums = json.load(f)
                        if cached_checksums == fileset.checksums:
                            need_to_download = False
                    except IOError:
                        pass
                else:
                    need_to_download = False
            if need_to_download:
                xresource = xscan.resources[fileset._resource_name]
                # The path to the directory which the files will be
                # downloaded to.
                tmp_dir = cache_path + '.download'
                try:
                    # Attempt to make tmp download directory. This will
                    # fail if another process (or previous attempt) has
                    # already created it. In that case this process will
                    # wait to see if that download finishes successfully,
                    # and if so use the cached version.
                    os.mkdir(tmp_dir)
                except OSError as e:
                    if e.errno == errno.EEXIST:
                        # Another process may be concurrently downloading
                        # the same file to the cache. Wait for
                        # 'race_cond_delay' seconds and then check that it
                        # has been completed or assume interrupted and
                        # redownload.
                        self._delayed_download(
                            tmp_dir, xresource, xscan, fileset,
                            xsession.label, cache_path,
                            delay=self._race_cond_delay)
                    else:
                        raise
                else:
                    self.download_fileset(
                        tmp_dir, xresource, xscan, fileset,
                        xsession.label, cache_path)
                    shutil.rmtree(tmp_dir)
        if not fileset.format.directory:
            (primary_path, aux_paths) = fileset.format.assort_files(
                op.join(cache_path, f) for f in os.listdir(cache_path))
        else:
            primary_path = cache_path
            aux_paths = None
        return primary_path, aux_paths

    def get_field(self, field):
        self._check_repository(field)
        with self:
            xsession = self.get_xsession(field)
            val = xsession.fields[self._field_name(field)]
            val = val.replace('&quot;', '"')
            val = parse_value(val)
        return val

    def put_fileset(self, fileset):
        if fileset.format is None:
            raise ArcanaFileFormatError(
                "Format of {} needs to be set before it is uploaded to {}"
                .format(fileset, self))
        self._check_repository(fileset)
        # Open XNAT session
        with self:
            # Add session for derived scans if not present
            xsession = self.get_xsession(fileset)
            cache_path = self._cache_path(fileset)
            # Make session cache dir
            cache_path_dir = (op.dirname(cache_path)
                              if fileset.format.directory else cache_path)
            if os.path.exists(cache_path_dir):
                shutil.rmtree(cache_path_dir)
            os.makedirs(cache_path_dir, stat.S_IRWXU | stat.S_IRWXG)
            if fileset.format.directory:
                shutil.copytree(fileset.path, cache_path)
            else:
                # Copy primary file
                shutil.copyfile(fileset.path,
                                op.join(cache_path, fileset.fname))
                # Copy auxiliaries
                for sc_fname, sc_path in fileset.aux_file_fnames_and_paths:
                    shutil.copyfile(sc_path, op.join(cache_path, sc_fname))
            with open(cache_path + XnatRepo.MD5_SUFFIX, 'w',
                      **JSON_ENCODING) as f:
                json.dump(fileset.calculate_checksums(), f, indent=2)
            # Upload to XNAT
            xscan = self.login.classes.MrScanData(
                id=fileset.id, type=fileset.basename, parent=xsession)
            fileset.uri = xscan.uri
            # Select the first xnat_resource name to use to upload the data to
            resource_name = fileset.format.resource_names(self.type)[0]
            try:
                xresource = xscan.resources[resource_name]
            except KeyError:
                pass
            else:
                # Delete existing resource
                # TODO: probably should have check to see if we want to
                #       override it
                xresource.delete()
            xresource = xscan.create_resource(resource_name)
            if fileset.format.directory:
                for dpath, _, fnames  in os.walk(fileset.path):
                    for fname in fnames:
                        fpath = op.join(dpath, fname)
                        frelpath = op.relpath(fpath, fileset.path)
                        xresource.upload(fpath, frelpath)
            else:
                xresource.upload(fileset.path, fileset.fname)
                for sc_fname, sc_path in fileset.aux_file_fnames_and_paths:
                    xresource.upload(sc_path, sc_fname)

    def put_field(self, field):
        self._check_repository(field)
        val = field.value
        if field.array:
            if field.dtype is str:
                val = ['"{}"'.format(v) for v in val]
            val = '[' + ','.join(str(v) for v in val) + ']'
        if field.dtype is str:
            val = '"{}"'.format(val)
        with self:
            xsession = self.get_xsession(field)
            xsession.fields[self._field_name(field)] = val

    def put_record(self, record, dataset):
        base_cache_path = self._cache_path(
            record, name=self.PROV_SCAN, dataset=dataset)
        if not op.exists(base_cache_path):
            os.mkdir(base_cache_path)
        else:
            if not op.isdir(base_cache_path):
                raise ArcanaError(
                    "Base provenance cache path ('{}') should be a directory"
                    .format(base_cache_path))
        cache_path = op.join(base_cache_path, record.pipeline_name + '.json')
        record.save(cache_path)
        # TODO: Should also save digest of prov.json to check to see if it
        #       has been altered remotely
        xsession = self.get_xsession(record, dataset=dataset)
        xprov = self.login.classes.MrScanData(
            id=self.PROV_SCAN, type=self.PROV_SCAN, parent=xsession)
        # Delete existing provenance if present
        try:
            xresource = xprov.resources[record.pipeline_name]
        except KeyError:
            pass
        else:
            xresource.delete()
        # FIXME: should reuse the same resource for all provenance jsons
        xresource = xprov.create_resource(record.pipeline_name)
        xresource.upload(cache_path, op.basename(cache_path))

    def get_checksums(self, fileset):
        """
        Downloads the MD5 digests associated with the files in the file-set.
        These are saved with the downloaded files in the cache and used to
        check if the files have been updated on the server

        Parameters
        ----------
        resource : xnat.ResourceCatalog
            The xnat resource
        file_format : FileFormat
            The format of the fileset to get the checksums for. Used to
            determine the primary file within the resource and change the
            corresponding key in the checksums dictionary to '.' to match
            the way it is generated locally by Arcana.
        """
        if fileset.uri is None:
            raise ArcanaUsageError(
                "Can't retrieve checksums as URI has not been set for {}"
                .format(fileset))
        with self:
            checksums = {r['Name']: r['digest']
                         for r in self.login.get_json(fileset.uri + '/files')[
                             'ResultSet']['Result']}
        if not fileset.format.directory:
            # Replace the key corresponding to the primary file with '.' to
            # match the way that checksums are created by Arcana
            primary = fileset.format.assort_files(checksums.keys())[0]
            checksums['.'] = checksums.pop(primary)
        return checksums

    def find_data(self, dataset, subject_ids=None, visit_ids=None, **kwargs):
        """
        Find all filesets, fields and provenance records within an XNAT project

        Parameters
        ----------
        subject_ids : list(str)
            List of subject IDs with which to filter the tree with. If
            None all are returned
        visit_ids : list(str)
            List of visit IDs with which to filter the tree with. If
            None all are returned

        Returns
        -------
        filesets : list[Fileset]
            All the filesets found in the repository
        fields : list[Field]
            All the fields found in the repository
        records : list[Record]
            The provenance records found in the repository
        """
        subject_ids = self.convert_subject_ids(subject_ids)
        # Add derived visit IDs to list of visit ids to filter
        filesets = []
        fields = []
        records = []
        project_id = dataset.name
        # Note we prefer the use of raw REST API calls here for performance
        # reasons over using XnatPy's data structures.
        with self:
            # Get per_dataset level derivatives and fields
            project_uri = '/data/archive/projects/{}'.format(project_id)
            project_json = self.login.get_json(project_uri)
            fields.extend(self.find_fields(
                project_json, dataset, frequency='per_dataset'))
            fsets, recs = self.find_derivatives(
                project_json, project_uri, dataset, frequency='per_dataset')
            filesets.extend(fsets)
            records.extend(recs)
            # Get map of internal subject IDs to subject labels in project
            subject_xids_to_labels = {
                s['ID']: s['label'] for s in self.login.get_json(
                    '/data/projects/{}/subjects'.format(project_id))[
                        'ResultSet']['Result']}
            # Get list of all sessions within project
            session_xids = [
                s['ID'] for s in self.login.get_json(
                    '/data/projects/{}/experiments'.format(project_id))[
                        'ResultSet']['Result']
                if (self.session_filter is None
                    or re.match(self.session_filter, s['label']))]
            subject_xids = set()
            for session_xid in tqdm(session_xids,
                                    "Scanning sessions in '{}' project"
                                    .format(project_id)):
                session_json = self.login.get_json(
                    '/data/projects/{}/experiments/{}'.format(
                        project_id, session_xid))['items'][0]
                subject_xid = session_json['data_fields']['subject_ID']
                subject_xids.add(subject_xids)
                session_uri = (
                    '/data/archive/projects/{}/subjects/{}/experiments/{}'
                    .format(project_id, subject_xid, session_xid))
                subject_id = subject_xids_to_labels[subject_xid]
                session_label = session_json['data_fields']['label']
                # Extract analysis name and derived-from session
                # Strip subject ID from session label if required
                if session_label.startswith(subject_id + '_'):
                    visit_id = session_label[len(subject_id) + 1:]
                else:
                    visit_id = session_label
                # Strip project ID from subject ID if required
                if subject_id.startswith(project_id + '_'):
                    subject_id = subject_id[len(project_id) + 1:]
                        # Extract part of JSON relating to files
                filesets.extend(self.find_scans(
                    session_json, session_uri, subject_id, visit_id,
                    dataset, **kwargs))
                fields.extend(self.find_fields(
                    session_json, dataset, subject_id=subject_id,
                    visit_id=visit_id, **kwargs))
                fsets, recs = self.find_derivatives(
                    session_json, session_uri, dataset, subject_id=subject_id,
                    visit_id=visit_id, frequency='per_session')
                filesets.extend(fsets)
                records.extend(recs)
            # Get subject level resources and fields
            for subject_xid in subject_xids:
                subject_id = subject_xids_to_labels[subject_xid]
                subject_uri = ('/data/archive/projects/{}/subjects/{}'
                               .format(project_id, subject_xid))
                subject_json = self.login.get_json(subject_uri)
                fields.extend(self.find_fields(
                    subject_json, dataset, frequency='per_subject',
                    subject_id=subject_id))
                fsets, recs = self.find_derivatives(
                    subject_json, subject_uri, dataset,
                    frequency='per_subject', subject_id=subject_id)
                filesets.extend(fsets)
                records.extend(recs)
        return filesets, fields, records

    def find_derivatives(self, node_json, node_uri, dataset, frequency,
                         subject_id=None, visit_id=None, **kwargs):
        try:
            resources_json = next(
                c['items'] for c in node_json['children']
                if c['field'] == 'resources/resource')
        except StopIteration:
            return []
        filesets = []
        records = []
        for d in resources_json:
            label = d['data_fields']['label']
            resource_uri = '{}/resources/{}'.format(node_uri, label)
            if '-' in label:
                label_parts = label.split('-')
                name = label_parts[1:]
                from_analysis = label_parts[0]
            else:
                name = label
                from_analysis = None
            if name != self.PROV_RESOURCE:
                filesets.append(Fileset(
                    name, uri=resource_uri, dataset=dataset,
                    from_analysis=from_analysis, frequency=frequency,
                    subject_id=subject_id, visit_id=visit_id,
                    resource_name=None, **kwargs))
            else:
                # Download provenance JSON files and parse into
                # records
                temp_dir = tempfile.mkdtemp()
                try:
                    with tempfile.TemporaryFile() as temp_zip:
                        self.login.download_stream(
                            resource_uri + '/files', temp_zip, format='zip')
                        with ZipFile(temp_zip) as zip_file:
                            zip_file.extractall(temp_dir)
                    for base_dir, _, fnames in os.walk(temp_dir):
                        for fname in fnames:
                            if fname.endswith('.json'):
                                pipeline_name = fname[:-len('.json')]
                                json_path = op.join(base_dir, fname)
                                records.append(
                                    Record.load(
                                        pipeline_name,
                                        path=json_path,
                                        frequency=frequency,
                                        subject_id=subject_id,
                                        visit_id=visit_id,
                                        from_analysis=from_analysis))
                finally:
                    shutil.rmtree(temp_dir, ignore_errors=True)
        return filesets, records

    def find_fields(self, node_json, dataset, frequency, subject_id=None,
                    visit_id=None, **kwargs):
        try:
            fields_json = next(
                c['items'] for c in node_json['children']
                if c['field'] == 'fields/field')
        except StopIteration:
            return []
        fields = []
        for js in fields_json:
            try:
                value = js['data_fields']['field']
            except KeyError:
                continue
            value = value.replace('&quot;', '"')
            name = js['data_fields']['name']
            names = [(name, None)]
            # If '-' is present in the name then add the field twice, once
            # as a field name in its own right and second as a field name
            # prefixed by an analysis name
            if '-' in name:
                parts = name.split('-')
                from_analysis = parts[0]
                name = '-'.join(parts[1:])
                names.append((name, from_analysis))
            for name, from_analysis in names:
                fields.append(Field(
                    name=name,
                    value=value,
                    from_analysis=from_analysis,
                    dataset=dataset,
                    subject_id=subject_id,
                    visit_id=visit_id,
                    frequency=frequency,
                    **kwargs))
        return fields

    def find_scans(self, session_json, session_uri, subject_id,
                   visit_id, dataset, **kwargs):
        try:
            scans_json = next(
                c['items'] for c in session_json['children']
                if c['field'] == 'scans/scan')
        except StopIteration:
            return []
        filesets = []
        for scan_json in scans_json:
            scan_id = scan_json['data_fields']['ID']
            scan_type = scan_json['data_fields'].get('type', '')
            scan_quality = scan_json['data_fields'].get('quality',
                                                        None)
            scan_uri = '{}/scans/{}'.format(session_uri, scan_id)
            try:
                resources_json = next(
                    c['items'] for c in scan_json['children']
                    if c['field'] == 'file')
            except StopIteration:
                resources = {}
            else:
                resources = {js['data_fields']['label']:
                             js['data_fields'].get('format', None)
                             for js in resources_json}
            # Remove auto-generated snapshots directory
            resources.pop('SNAPSHOTS', None)
            for resource in resources:
                filesets.append(Fileset(
                    scan_type, id=scan_id, uri=scan_uri,
                    dataset=dataset, subject_id=subject_id, visit_id=visit_id,
                    quality=scan_quality, resource_name=resource, **kwargs))
        logger.debug("Found node %s:%s", subject_id, visit_id)
        return filesets

    def convert_subject_ids(self, subject_ids):
        """
        Convert subject ids to strings if they are integers
        """
        # TODO: need to make this generalisable via a
        #       splitting+mapping function passed to the repository
        if subject_ids is not None:
            subject_ids = set(
                ('{:03d}'.format(s)
                 if isinstance(s, int) else s) for s in subject_ids)
        return subject_ids

    def extract_subject_id(self, xsubject_label):
        """
        This assumes that the subject ID is prepended with
        the project ID.
        """
        return xsubject_label.split('_')[1]

    def extract_visit_id(self, xsession_label):
        """
        This assumes that the session ID is preprended
        """
        return '_'.join(xsession_label.split('_')[2:])

    def dicom_header(self, fileset):
        def convert(val, code):
            if code == 'TM':
                try:
                    val = float(val)
                except ValueError:
                    pass
            elif code == 'CS':
                val = val.split('\\')
            return val
        with self:
            response = self.login.get(
                '/REST/services/dicomdump?src='
                + fileset.uri[len('/data'):]).json()['ResultSet']['Result']
        hdr = {tag_parse_re.match(t['tag1']).groups(): convert(t['value'],
                                                               t['vr'])
               for t in response if (tag_parse_re.match(t['tag1'])
                                     and t['vr'] in RELEVANT_DICOM_TAG_TYPES)}
        return hdr

    def download_fileset(self, tmp_dir, xresource, xscan, fileset,
                         session_label, cache_path):
        # Download resource to zip file
        zip_path = op.join(tmp_dir, 'download.zip')
        with open(zip_path, 'wb') as f:
            xresource.xnat_session.download_stream(
                xresource.uri + '/files', f, format='zip', verbose=True)
        checksums = self.get_checksums(fileset)
        # Extract downloaded zip file
        expanded_dir = op.join(tmp_dir, 'expanded')
        try:
            with ZipFile(zip_path) as zip_file:
                zip_file.extractall(expanded_dir)
        except BadZipfile as e:
            raise ArcanaError(
                "Could not unzip file '{}' ({})"
                .format(xresource.id, e))
        data_path = op.join(
            expanded_dir, session_label, 'scans',
            (xscan.id + '-' + special_char_re.sub('_', xscan.type)),
            'resources', xresource.label, 'files')
        # Remove existing cache if present
        try:
            shutil.rmtree(cache_path)
        except OSError as e:
            if e.errno != errno.ENOENT:
                raise e
        shutil.move(data_path, cache_path)
        with open(cache_path + XnatRepo.MD5_SUFFIX, 'w',
                  **JSON_ENCODING) as f:
            json.dump(checksums, f, indent=2)

    def _delayed_download(self, tmp_dir, xresource, xscan, fileset,
                          session_label, cache_path, delay):
        logger.info("Waiting {} seconds for incomplete download of '{}' "
                    "initiated another process to finish"
                    .format(delay, cache_path))
        initial_mod_time = dir_modtime(tmp_dir)
        time.sleep(delay)
        if op.exists(cache_path):
            logger.info("The download of '{}' has completed "
                        "successfully in the other process, continuing"
                        .format(cache_path))
            return
        elif initial_mod_time != dir_modtime(tmp_dir):
            logger.info(
                "The download of '{}' hasn't completed yet, but it has"
                " been updated.  Waiting another {} seconds before "
                "checking again.".format(cache_path, delay))
            self._delayed_download(tmp_dir, xresource, xscan,
                                   fileset,
                                   session_label, cache_path, delay)
        else:
            logger.warning(
                "The download of '{}' hasn't updated in {} "
                "seconds, assuming that it was interrupted and "
                "restarting download".format(cache_path, delay))
            shutil.rmtree(tmp_dir)
            os.mkdir(tmp_dir)
            self.download_fileset(
                tmp_dir, xresource, xscan, fileset, session_label,
                cache_path)

    def get_xsession(self, item, dataset=None):
        """
        Returns the XNAT session and cache dir corresponding to the
        item.
        """
        if dataset is None:
            dataset = item.dataset
        subj_label = dataset.subject_label(item.subject_id)
        sess_label = dataset.session_label(item.subject_id, item.visit_id)
        with self:
            xproject = self.login.projects[dataset.name]
            try:
                xsubject = xproject.subjects[subj_label]
            except KeyError:
                xsubject = self.login.classes.SubjectData(
                    label=subj_label, parent=xproject)
            try:
                xsession = xsubject.experiments[sess_label]
            except KeyError:
                xsession = self.login.classes.MrSessionData(
                    label=sess_label, parent=xsubject)
        return xsession

    def _cache_path(self, fileset, name=None, dataset=None):
        if dataset is None:
            dataset = fileset.dataset
        subj_dir = dataset.subject_label(fileset.subject_id)
        sess_dir = dataset.session_label(fileset.subject_id, fileset.visit_id)
        cache_dir = op.join(self.cache_dir, dataset.name, subj_dir, sess_dir)
        makedirs(cache_dir, exist_ok=True)
        if fileset.derived:
            name = fileset.name if name is None else name
            cache_path = op.join(cache_dir, '__resources__', name)
        else:
            if name is None:
                name = '{}-{}'.format(fileset.id,
                                      special_char_re.sub('_', fileset.name))
            cache_path = op.join(cache_dir, name)
        return cache_path

    def _check_repository(self, item):
        if item.dataset.repository is not self:
            raise ArcanaWrongRepositoryError(
                "{} is from {} instead of {}".format(
                    item, item.dataset.repository, self))

    @classmethod
    def _field_name(cls, field):
        if field.derived:
            name = field.from_analysis + '-' + field.name
        else:
            name = field.name
        return name
