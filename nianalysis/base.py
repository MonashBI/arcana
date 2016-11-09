import os.path
from nianalysis.formats import DatasetFormat
from copy import copy
from nipype.interfaces.base import traits
from nianalysis.formats import dataset_formats


class Dataset(object):
    """
    A class representing either an "acquired dataset", which was acquired
    externally, or a "processed dataset", which was generated by a processing
    pipeline. It is also used as a placeholder in the Project classes to
    specify which datasets (components) are expected to be provided ("acquired
    datasets") or will be generated by the pipelines associated with the project
    ("generated datasets").

    Parameters
    ----------
    name : str
        The name of the dataset
    format : FileFormat
        The file format used to store the dataset. Can be one of the
        recognised formats
    pipeline : Project.method
        The method of the project that is used to generate the dataset. If None
        the dataset is assumed to be acquired externall
    multiplicity : str
        One of 'per_subject', 'subject_subset', and 'per_project', specifying
        whether the dataset is present for each session, subject or project.
    """

    MULTIPLICITY_OPTIONS = ('per_session', 'per_subject', 'per_project')
    #                        'per_session_subset', 'per_subject_subset')

    def __init__(self, name, format=None, pipeline=None,  # @ReservedAssignment @IgnorePep8
                 multiplicity='per_session'):
        assert isinstance(name, basestring)
        assert isinstance(format, DatasetFormat)
        assert multiplicity in self.MULTIPLICITY_OPTIONS
        self._name = name
        self._format = format
        self._pipeline = pipeline
        self._multiplicity = multiplicity
        self._prefix = ''

    def __eq__(self, other):
        try:
            return (self.name == other.name and
                    self.format == other.format and
                    self.pipeline == other.pipeline and
                    self.multiplicity == other.multiplicity and
                    self._prefix == other._prefix)
        except AttributeError as e:
            assert not e.message.startswith(
                "'{}'".format(self.__class__.__name__))
            return False

    def __ne__(self, other):
        return not (self == other)

    @property
    def name(self):
        return self._name

    @property
    def format(self):
        return self._format

    @property
    def pipeline(self):
        return self._pipeline

    @property
    def processed(self):
        return self._pipeline is not None

    @property
    def multiplicity(self):
        return self._multiplicity

    def __iter__(self):
        return iter(self.as_tuple())

    def to_tuple(self):
        return self.name, self.format.name, self.multiplicity, self.processed

    @classmethod
    def from_tuple(cls, tple):
        name, format_name, multiplicity, processed = tple
        dataset_format = dataset_formats[format_name]
        return cls(name, dataset_format, pipeline=processed,
                   multiplicity=multiplicity)

    @classmethod
    def traits_spec(self):
        """
        Return the specification for a Dataset as a tuple
        """
        return traits.Tuple(  # @UndefinedVariable
            traits.Str(  # @UndefinedVariable
                mandatory=True,
                desc="name of file"),
            traits.Str(  # @UndefinedVariable
                mandatory=True,
                desc="name of the dataset format"),
            traits.Str(mandatory=True,  # @UndefinedVariable @IgnorePep8
                       desc="multiplicity of the dataset (one of '{}')".format(
                            "', '".join(self.MULTIPLICITY_OPTIONS))),
            traits.Bool(mandatory=True,  # @UndefinedVariable @IgnorePep8
                        desc=("whether the file was generate by a pipeline "
                              "or not")))

    @property
    def filename(self, format=None):  # @ReservedAssignment
        if format is None:
            assert self.format is not None, "Dataset format is undefined"
            format = self.format  # @ReservedAssignment
        return self._prefix + self.name + format.extension

    def match(self, filename):
        base, ext = os.path.splitext(filename)
        return base == self.name and (ext == self.format.extension or
                                      self.format is None)

    def apply_prefix(self, prefix):
        """Duplicate the dataset and provide a prefix to apply to the filename"""
        duplicate = copy(self)
        duplicate._prefix = prefix
        return duplicate

    def __repr__(self):
        return ("Dataset(name='{}', format={}, pipeline={})"
                .format(self.name, self.format, self.pipeline))
