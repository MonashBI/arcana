from past.builtins import basestring
from builtins import object
from arcana.exceptions import (
    ArcanaMissingDataException, ArcanaNameError)
from arcana.exceptions import ArcanaUsageError
from .base import Study, StudyMetaClass


class MultiStudy(Study):
    """
    Abstract base class for all studies that combine multiple studies
    into a "multi-study".

    Parameters
    ----------
    name : str
        The name of the combined study.
    repository : Repository
        An Repository object that provides access to a DaRIS, XNAT or
        local file system
    processor : Processor
        The processor the processes the derived data when demanded
    inputs : Dict[str, Fileset|Field]
        A dict containing the a mapping between names of study data_specs
        and existing filesets (typically primary from the scanner but can
        also be replacements for generated data_specs)
    parameters : List[Parameter] | Dict[str, (int|float|str)]
        Parameters that are passed to pipelines when they are constructed
        either as a dictionary of key-value pairs or as a list of
        'Parameter' objects. The name and dtype must match ParamSpecs in
        the _param_spec class attribute (see 'add_param_specs').
    subject_ids : List[(int|str)]
        List of subject IDs to restrict the analysis to
    visit_ids : List[(int|str)]
        List of visit IDs to restrict the analysis to
    check_inputs : bool
        Whether to check the inputs to see if any acquired filesets
        are missing
    reprocess : bool
        Whether to reprocess fileset|fields that have been created with
        different parameters and/or pipeline-versions. If False then
        and exception will be thrown if the repository already contains
        matching filesets|fields created with different parameters.

    Class Attrs
    -----------
    add_substudy_specs : list[SubStudySpec]
        Subclasses of MultiStudy typically have a 'add_substudy_specs'
        class member, which defines the sub-studies that make up the
        combined study and the mapping of their fileset names. The key
        of the outer dictionary will be the name of the sub-study, and
        the value is a tuple consisting of the class of the sub-study
        and a map of fileset names from the combined study to the
        sub-study e.g.

            add_substudy_specs = [
                SubStudySpec('t1_study', T1Study, {'magnitude': 't1'}),
                SubStudySpec('t2_study', T2Study, {'magnitude': 't2'})]

            add_data_specs = [
                FilesetSpec('t1', text_format'),
                FilesetSpec('t2', text_format')]
    add_data_specs : List[FilesetSpec|FieldSpec]
        Add's that data specs to the 'data_specs' class attribute,
        which is a dictionary that maps the names of filesets that are
        used and generated by the study to FilesetSpec objects.
    add_param_specs : List[ParamSpec]
        Default parameters for the class
    """

    _substudy_specs = {}

    implicit_cls_attrs = Study.implicit_cls_attrs + ['_substudy_specs']

    def __init__(self, name, repository, processor, inputs,
                 parameters=None, **kwargs):
        try:
            # This works for PY3 as the metaclass inserts it itself if
            # it isn't provided
            metaclass = type(self).__dict__['__metaclass__']
            if not issubclass(metaclass, MultiStudyMetaClass):
                raise KeyError
        except KeyError:
            raise ArcanaUsageError(
                "Need to set MultiStudyMetaClass (or sub-class) as "
                "the metaclass of all classes derived from "
                "MultiStudy")
        super(MultiStudy, self).__init__(
            name, repository, processor, inputs, parameters=parameters,
            **kwargs)
        self._substudies = {}
        for substudy_spec in self.substudy_specs():
            substudy_cls = substudy_spec.study_class
            # Map inputs, data_specs to the substudy
            mapped_inputs = {}
            for data_name in substudy_cls.data_spec_names():
                mapped_name = substudy_spec.map(data_name)
                if mapped_name in self.input_names:
                    mapped_inputs[data_name] = self.input(mapped_name)
                else:
                    try:
                        inpt = self.spec(mapped_name)
                    except ArcanaMissingDataException:
                        pass
                    else:
                        if inpt.derived:
                            mapped_inputs[data_name] = inpt
            # Map parameters to the substudy
            mapped_parameters = {}
            for param_name in substudy_cls.param_spec_names():
                mapped_name = substudy_spec.map(param_name)
                parameter = self._get_parameter(mapped_name)
                mapped_parameters[param_name] = parameter
            # Create sub-study
            substudy = substudy_spec.study_class(
                name + '_' + substudy_spec.name,
                repository, processor, mapped_inputs,
                parameters=mapped_parameters, enforce_inputs=False,
                subject_ids=self.subject_ids, visit_ids=self.visit_ids,
                clear_caches=False)
            # Append to dictionary of substudies
            if substudy_spec.name in self._substudies:
                raise ArcanaNameError(
                    substudy_spec.name,
                    "Duplicate sub-study names '{}'"
                    .format(substudy_spec.name))
            self._substudies[substudy_spec.name] = substudy

    @property
    def substudies(self):
        return iter(self._substudies.values())

    @property
    def substudy_names(self):
        return iter(self._substudies.keys())

    def substudy(self, name):
        try:
            return self._substudies[name]
        except KeyError:
            raise ArcanaNameError(
                name,
                "'{}' not found in sub-studes ('{}')"
                .format(name, "', '".join(self._substudies)))

    @classmethod
    def substudy_spec(cls, name):
        try:
            return cls._substudy_specs[name]
        except KeyError:
            raise ArcanaNameError(
                name,
                "'{}' not found in sub-studes ('{}')"
                .format(name, "', '".join(cls._substudy_specs)))

    @classmethod
    def substudy_specs(cls):
        return iter(cls._substudy_specs.values())

    @classmethod
    def substudy_spec_names(cls):
        return iter(cls._substudy_specs.keys())

    def __repr__(self):
        return "{}(name='{}')".format(
            self.__class__.__name__, self.name)

    @classmethod
    def translate(cls, substudy_name, pipeline_getter, pipeline_arg_names=(),
                  auto_added=False):
        """
        A method for translating pipeline constructors from a sub-study to the
        namespace of a multi-study. Returns a new method that calls the
        sub-study pipeline constructor with appropriate keyword arguments

        Parameters
        ----------
        substudy_name : str
            Name of the sub-study
        pipeline_getter : str
            Name of method used to construct the pipeline in the sub-study
        pipeline_arg_names : tuple[str]
            Names of pipeline arguments passed to the method
        auto_added : bool
            Signify that a method was automatically added by the
            MultiStudyMetaClass. Used in checks when pickling Study
            objects
        """
        assert isinstance(substudy_name, basestring)
        assert isinstance(pipeline_getter, basestring)

        def translated_getter(self, **kwargs):
            substudy_spec = self.substudy_spec(substudy_name)
            pipeline_args = {n: kwargs.pop(n) for n in pipeline_arg_names}
            # Combine mapping of names of sub-study specs with
            return getattr(self.substudy(substudy_name), pipeline_getter)(
                prefix=substudy_name + '_',
                input_map=substudy_spec.name_map,
                output_map=substudy_spec.name_map,
                study=self, name_maps=kwargs,
                **pipeline_args)
        # Add reduce method to allow it to be pickled
        translated_getter.auto_added = auto_added
        return translated_getter


class SubStudySpec(object):
    """
    Specify a study to be included in a MultiStudy class

    Parameters
    ----------
    name : str
        Name for the sub-study
    study_class : type (sub-classed from Study)
        The class of the sub-study
    name_map : dict[str, str]
        A mapping of fileset/field/parameter names from the sub-study
        namespace to the namespace of the MultiStudy. All data-specs
        that are not explicitly mapped are auto-translated using
        the sub-study prefix (name + '_').
    """

    def __init__(self, name, study_class, name_map=None):
        self._name = name
        self._study_class = study_class
        # Fill fileset map with default values before overriding with
        # argument provided to constructor
        self._name_map = name_map if name_map is not None else {}

    @property
    def name(self):
        return self._name

    def __repr__(self):
        return "{}(name='{}', cls={}, name_map={}".format(
            type(self).__name__, self.name, self.study_class,
            self._name_map)

    @property
    def study_class(self):
        return self._study_class

    @property
    def name_map(self):
        nmap = dict((s.name, self.apply_prefix(s.name))
                    for s in self.auto_data_specs)
        nmap.update(self._name_map)
        return nmap

    def map(self, name):
        try:
            return self._name_map[name]
        except KeyError:
            if name not in self.study_class.spec_names():
                raise ArcanaNameError(
                    name,
                    "'{}' doesn't match any filesets, fields, parameters "
                    "in the study class {} ('{}')"
                    .format(name, self.name,
                            self.study_class.__name__,
                            "', '".join(self.study_class.spec_names())))
            return self.apply_prefix(name)

    def apply_prefix(self, name):
        return self.name + '_' + name

    @property
    def auto_data_specs(self):
        """
        Data specs in the sub-study class that are not explicitly provided
        in the name map
        """
        for spec in self.study_class.data_specs():
            if spec.name not in self._name_map:
                yield spec

    @property
    def auto_param_specs(self):
        """
        Parameter pecs in the sub-study class that are not explicitly provided
        in the name map
        """
        for spec in self.study_class.param_specs():
            if spec.name not in self._name_map:
                yield spec


class MultiStudyMetaClass(StudyMetaClass):
    """
    Metaclass for "multi" study classes that automatically adds
    translated data specs and pipelines from sub-study specs if they
    are not explicitly mapped in the spec.
    """

    def __new__(metacls, name, bases, dct):  # @NoSelf @UnusedVariable
        if not any(issubclass(b, MultiStudy) for b in bases):
            raise ArcanaUsageError(
                "MultiStudyMetaClass can only be used for classes that "
                "have MultiStudy as a base class")
        try:
            add_substudy_specs = dct['add_substudy_specs']
        except KeyError:
            add_substudy_specs = dct['add_substudy_specs'] = []
        dct['_substudy_specs'] = substudy_specs = {}
        for base in reversed(bases):
            try:
                substudy_specs.update(
                    (d.name, d) for d in base.substudy_specs())
            except AttributeError:
                pass
        substudy_specs.update(
            (s.name, s) for s in add_substudy_specs)
        if '__metaclass__' not in dct:
            dct['__metaclass__'] = metacls
        cls = StudyMetaClass(name, bases, dct)
        # Loop through all data specs that haven't been explicitly
        # mapped and add a data spec in the multi class.
        for substudy_spec in list(substudy_specs.values()):
            # Map data specs
            for data_spec in substudy_spec.auto_data_specs:
                trans_sname = substudy_spec.apply_prefix(
                    data_spec.name)
                if trans_sname not in cls.data_spec_names():
                    initkwargs = data_spec.initkwargs()
                    initkwargs['name'] = trans_sname
                    if data_spec.derived:
                        trans_pname = substudy_spec.apply_prefix(
                            data_spec.pipeline_getter)
                        initkwargs['pipeline_getter'] = trans_pname
                        # Check to see whether pipeline has already been
                        # translated or always existed in the class (when
                        # overriding default parameters for example)
                        if not hasattr(cls, trans_pname):
                            setattr(cls, trans_pname,
                                    MultiStudy.translate(
                                        substudy_spec.name,
                                        data_spec.pipeline_getter,
                                        pipeline_arg_names=
                                        data_spec.pipeline_arg_names,
                                        auto_added=True))
                    trans_data_spec = type(data_spec)(**initkwargs)
                    # Allow the default input (e.g. an atlas) to translate
                    # any parameter names it needs to use
                    if not data_spec.derived and data_spec.default is not None:
                        try:
                            trans_data_spec.default.translate(substudy_spec)
                        except AttributeError:
                            pass
                    cls._data_specs[trans_sname] = trans_data_spec
            # Map parameter specs
            for param_spec in substudy_spec.auto_param_specs:
                trans_sname = substudy_spec.apply_prefix(
                    param_spec.name)
                if trans_sname not in cls.param_spec_names():
                    renamed_spec = param_spec.renamed(trans_sname)
                    cls._param_specs[
                        renamed_spec.name] = renamed_spec
        # Check all names in name-map correspond to data or parameter
        # specs
        for substudy_spec in list(substudy_specs.values()):
            local_spec_names = list(
                substudy_spec.study_class.spec_names())
            for (local_name,
                 global_name) in substudy_spec._name_map.items():
                if local_name not in local_spec_names:
                    raise ArcanaUsageError(
                        "'{}' in name-map for '{}' sub study spec in {}"
                        "MultiStudy class does not name a spec in {} "
                        "class:\n{}"
                        .format(local_name, substudy_spec.name,
                                name, substudy_spec.study_class,
                                '\n'.join(local_spec_names)))
                if global_name not in cls.spec_names():
                    raise ArcanaUsageError(
                        "'{}' in name-map for '{}' sub study spec in {}"
                        "MultiStudy class does not name a spec:\n{}"
                        .format(global_name, substudy_spec.name, name,
                                '\n'.join(cls.spec_names())))
        return cls
