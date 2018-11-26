from past.builtins import basestring
import json
import re
from copy import deepcopy
from pprint import pformat
from datetime import datetime
from deepdiff import DeepDiff
from arcana.exceptions import ArcanaError, ArcanaUsageError
from arcana.pkg_info import install_requires


PROVENANCE_VERSION = '1.0'

ARCANA_DEPENDENCIES = [re.split(r'[><=]+', r)[0] for r in install_requires]


class Record(object):
    """
    A representation of the information required to describe the provenance of
    study derivatives. Records the provenance information relevant to a
    specific session, i.e. the general configuration of the pipeline and file
    checksums|field values of the pipeline inputs used to derive the outputs in
    a given session (or visit, subject, study summary). It also records the
    checksums|values of the outputs in order to detect if they have been
    altered outside of Arcana's management (e.g. manual QC/correction)

    Parameters
    ----------
    pipeline_name : str
        Name of the pipeline the record corresponds to
    frequency : str
        The frequency of the record
    subject_id : str | None
        The subject ID the record corresponds to. If None can be a per-visit or
        per-study summary
    visit_id : str | None
        The visit ID the record corresponds to. If None can be a per-subject or
        per-study summary
    from_study : str
        Name of the study that the record was generated by
    prov : dict[str, *]
        A dictionary containing the provenance recorded/to record
    """

    # For duck-typing with Filesets and Fields
    derived = True

    def __init__(self, pipeline_name, frequency, subject_id, visit_id,
                 from_study, prov):
        self._prov = deepcopy(prov)
        self._pipeline_name = pipeline_name
        self._frequency = frequency
        self._subject_id = subject_id
        self._visit_id = visit_id
        self._from_study = from_study
        if 'datetime' not in self._prov:
            self._prov['datetime'] = datetime.now().isoformat()

    def __repr__(self):
        return ("{}(pipeline={}, frequency={}, subject_id={}, visit_id={}, "
                "from_study='{}')".format(
                    type(self).__name__, self.pipeline_name, self.frequency,
                    self.subject_id, self.visit_id, self.from_study))

    def __eq__(self, other):
        return (self._prov == other._prov and
                self._frequency == other._frequency and
                self._subject_id == other._subject_id and
                self._visit_id == other._visit_id and
                self._from_study == other._from_study)

    @property
    def pipeline_name(self):
        return self._pipeline_name

    @property
    def prov(self):
        return self._prov

    @property
    def inputs(self):
        return self._prov['inputs']

    @property
    def outputs(self):
        return self._prov['outputs']

    @property
    def subject_id(self):
        return self._subject_id

    @property
    def visit_id(self):
        return self._visit_id

    @property
    def from_study(self):
        return self._from_study

    @property
    def frequency(self):
        return self._frequency

    @property
    def datetime(self):
        return self._prov['datetime']

    @property
    def provenance_version(self):
        return self._provenance_version

    def save(self, path):
        """
        Saves the provenance object to a JSON file, optionally including
        checksums for inputs and outputs (which are initially produced mid-
        run) to insert during the write

        Parameters
        ----------
        path : str
            Path to save the generated JSON file
        inputs : dict[str, str | list[str] | list[list[str]]] | None
            Checksums of all pipeline inputs used by the pipeline. For inputs
            of matching frequency to the output derivative associated with the
            provenance object, the values of the dictionary will be single
            checksums. If the output is of lower frequency they will be lists
            of checksums or in the case of 'per_session' inputs to 'per_study'
            outputs, lists of lists of checksum. They need to be provided here
            if the provenance object was initialised without checksums
        outputs : dict[str, str] | None
            Checksums of all pipeline outputs. They need to be provided here
            if the provenance object was initialised without checksums
        """
        with open(path, 'w') as f:
            try:
                json.dump(self.prov, f, indent=2)
            except TypeError:
                raise ArcanaError(
                    "Could not serialise provenance record dictionary:\n{}"
                    .format(pformat(self.prov)))

    @classmethod
    def load(cls, pipeline_name, frequency, subject_id, visit_id, from_study,
             path):
        """
        Loads a saved provenance object from a JSON file

        Parameters
        ----------
        path : str
            Path to the provenance file
        frequency : str
            The frequency of the record
        subject_id : str | None
            The subject ID of the provenance record
        visit_id : str | None
            The visit ID of the provenance record
        from_study : str
            Name of the study the derivatives were created for

        Returns
        -------
        record : Record
            The loaded provenance record
        """
        with open(path) as f:
            prov = json.load(f)
        return Record(pipeline_name, frequency, subject_id, visit_id,
                      from_study, prov)

    def mismatches(self, other, include=None, exclude=None):
        """
        Compares information stored within provenance objects with the
        exception of version information to see if they match. Matches are
        constrained to the paths passed to the 'include' kwarg, with the
        exception of sub-paths passed to the 'exclude' kwarg

        Parameters
        ----------
        other : Provenance
            The provenance object to compare against
        include : list[list[str]] | None
            Paths in the provenance to include in the match. If None all are
            incluced
        exclude : list[list[str]] | None
            Paths in the provenance to exclude from the match. In None all are
            excluded
        """
        if include is not None:
            include_res = []
            for path in include:
                if isinstance(path, basestring):
                    include_res.append(re.compile(
                        r"root\['{}'\].*"
                        .format(r"'\]\['".join(path.split('/')))))
                elif not isinstance(path, re.Pattern):
                    raise ArcanaUsageError(
                        "Include paths can either be path strings or regexes, "
                        "not '{}'".format(path))
        if exclude is not None:
            exclude_res = []
            for path in exclude:
                if isinstance(path, basestring):
                    exclude_res.append(re.compile(
                        r"root\['{}'\].*"
                        .format(r"'\]\['".join(path.split('/')))))
                elif not isinstance(path, re.Pattern):
                    raise ArcanaUsageError(
                        "Exclude paths can either be path strings or regexes, "
                        "not '{}'".format(path))
        diff = DeepDiff(self._prov, other._prov, ignore_order=True)
        # Create regular expresssions for the include and exclude paths in
        # the format that deepdiff uses for nested dictionary/lists

        def include_change(change):
            if include is None:
                included = True
            else:
                included = any(rx.match(change) for rx in include_res)
            if included and exclude is not None:
                included = not any(rx.match(change) for rx in exclude_res)
            return included

        filtered_diff = {}
        for change_type, changes in diff.items():
            if isinstance(changes, dict):
                filtered = dict((k, v) for k, v in changes.items()
                                if include_change(k))
            else:
                filtered = [c for c in changes if include_change(c)]
            if filtered:
                filtered_diff[change_type] = filtered
        return filtered_diff
