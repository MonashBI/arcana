from abc import ABCMeta
from copy import copy
from nipype.interfaces.utility import IdentityInterface
from nianalysis.exceptions import NiAnalysisMissingDatasetError
from nianalysis.pipeline import Pipeline
from .base import Study


class CombinedStudy(Study):
    """
    Abstract base class for all studies that combine multiple studies into a
    a combined study

    Parameters
    ----------
    name : str
        The name of the combined study.
    project_id: str
        The ID of the archive project from which to access the data from. For
        DaRIS it is the project id minus the proceeding 1008.2. For XNAT it
        will be the project code. For local archives name of the directory.
    archive : Archive
        An Archive object that provides access to a DaRIS, XNAT or local file
        system
    inputs : Dict[str, base.Dataset]
        A dict containing the a mapping between names of study data_specs
        and existing datasets (typically primary from the scanner but can
        also be replacements for generated data_specs)

    Required Sub-Class attributes
    -----------------------------
    sub_study_specs : dict[str, (type(Study), dict[str, str])
        Subclasses of CombinedStudy are expected to have a 'sub_study_specs'
        class member, which defines the sub-studies that make up the combined
        study and the mapping of their dataset names. The key of the outer
        dictionary will be the name of the sub-study, and the value is a tuple
        consisting of the class of the sub-study and a map of dataset names
        from the combined study to the sub-study e.g.

            sub_study_specs = {'t1_study': (MRIStudy, {'t1': 'mr_scan'}),
                               't2_study': (MRIStudy, {'t2': 'mr_scan'})}

            data_specs = set_data_specs(
                DatasetSpec('t1', nifti_gz_format'),
                DatasetSpec('t2', nifti_gz_format'))
    """

    __metaclass__ = ABCMeta

    def __init__(self, name, project_id, archive, inputs, **kwargs):
        super(CombinedStudy, self).__init__(name, project_id, archive,
                                            inputs, **kwargs)
        self._sub_studies = {}
        for (sub_study_name,
             (cls, dataset_map)) in self.sub_study_specs.iteritems():
            # Create copies of the input datasets to pass to the __init__
            # method of the generated sub-studies
            mapped_inputs = {}
            for n, inpt in inputs.iteritems():
                try:
                    mapped_inputs[dataset_map[n]] = inpt
                except KeyError:
                    pass  # Ignore datasets that are not required for sub-study
            # Create sub-study
            sub_study = cls(name + '_' + sub_study_name, project_id,
                            archive, mapped_inputs, check_inputs=False)
            # Set sub-study as attribute
            setattr(self, sub_study_name, sub_study)
            # Append to dictionary of sub_studies
            assert sub_study_name not in self._sub_studies, (
                "duplicate sub-study names '{}'".format(sub_study_name))
            self._sub_studies[sub_study_name] = sub_study

    @property
    def sub_studies(self):
        return self._sub_studies.itervalues()

    @classmethod
    def translate(cls, sub_study_name, pipeline_getter, **kwargs):
        """
        A "decorator" (although not intended to be used with @) for translating
        pipeline getter methods from a sub-study of a CombinedStudy.
        Returns a new method that calls the getter on the specified sub-
        sub-study then translates the pipeline to the CombinedStudy.

        Parameters
        ----------
        sub_study_name : str
            Name of the sub-study
        pipeline_getter : Study.method
            Unbound method used to create the pipeline in the sub-study
        """
        def translated_getter(self, **options):
            trans_pipeline = self.TranslatedPipeline(
                self, self._sub_studies[sub_study_name],
                pipeline_getter, options, **kwargs)
            trans_pipeline.assert_connected()
            return trans_pipeline
        return translated_getter

    class TranslatedPipeline(Pipeline):
        """
        A pipeline that is translated from a sub-study to the combined study.
        It takes the untranslated pipeline as an input and destroys it in the
        process

        Parameters
        ----------
        name : str
            Name of the translated pipeline
        pipeline : Pipeline
            Sub-study pipeline to translate
        combined_study : CombinedStudy
            Study to translate the pipeline to
        add_inputs : list[str]
            List of additional inputs to add to the translated pipeline to be
            connected manually in combined-study getter (i.e. not using
            translate_getter decorator).
        add_outputs : list[str]
            List of additional outputs to add to the translated pipeline to be
            connected manually in combined-study getter (i.e. not using
            translate_getter decorator).
        """

        def __init__(self, combined_study, sub_study, pipeline_getter,
                     options, add_inputs=None, add_outputs=None,
                     override_default_options=None):
            # Get the relative name of the sub-study (i.e. without the
            # combined study name prefixed)
            ss_name = sub_study.name[(len(combined_study.name) + 1):]
            # Override default options
            if override_default_options is not None:
                for n, val in override_default_options.items():
                    if n not in options:
                        options[n] = val
            # Create pipeline and overriding its name to include prefix
            # Copy across default options and override with extra
            # provided
            pipeline = pipeline_getter(
                sub_study, __name_prefix__=ss_name, **options)
            try:
                assert isinstance(pipeline, Pipeline)
            except Exception:
                raise
            self._options = pipeline._options
            self._name = pipeline.name
            self._study = combined_study
            self._workflow = pipeline.workflow
            ss_cls, dataset_map = combined_study.sub_study_specs[
                ss_name]
            inv_dataset_map = dict((v, combined_study.dataset_spec(k))
                                    for k, v in dataset_map.iteritems())
            assert isinstance(pipeline.study, ss_cls)
            # Translate inputs from sub-study pipeline
            try:
                self._inputs = [inv_dataset_map[i]
                                for i in pipeline.input_names]
            except KeyError as e:
                raise NiAnalysisMissingDatasetError(
                    "{} input required for pipeline '{}' in '{}' is not "
                    "present in dataset map:\n{}".format(
                        e, pipeline.name, ss_name, dataset_map))
            # Add additional inputs
            self._unconnected_inputs = set()
            if add_inputs is not None:
                self._check_spec_names(add_inputs, 'additional inputs')
                self._inputs.extend(add_inputs)
                self._unconnected_inputs.update(i.name
                                                for i in add_inputs)
            # Create new input node
            self._inputnode = self.create_node(
                IdentityInterface(fields=list(self.input_names)),
                name="inputnode_wrapper")
            # Connect to sub-study input node
            for input_name in pipeline.input_names:
                self.workflow.connect(
                    self._inputnode, inv_dataset_map[input_name].name,
                    pipeline.inputnode, input_name)
            # Translate outputs from sub-study pipeline
            self._outputs = {}
            for mult in pipeline.mutliplicities:
                try:
                    self._outputs[mult] = [
                        inv_dataset_map[o_name]
                        for o_name in pipeline.multiplicity_output_names(mult)]
                except KeyError as e:
                    raise NiAnalysisMissingDatasetError(
                        "'{}' output required for pipeline '{}' in '{}' is not"
                        " present in inverse dataset map:\n{}".format(
                            e, pipeline.name, ss_name,
                            inv_dataset_map))
            # Add additional outputs
            self._unconnected_outputs = set()
            if add_outputs is not None:
                self._check_spec_names(add_outputs, 'additional outputs')
                for output in add_outputs:
                    combined_study.dataset_spec(output).multiplicity
                    self._outputs[mult].append(output)
                self._unconnected_outputs.update(o.name for o in add_outputs)
            # Create output nodes for each multiplicity
            self._outputnodes = {}
            for mult in pipeline.mutliplicities:
                self._outputnodes[mult] = self.create_node(
                    IdentityInterface(
                        fields=list(self.multiplicity_output_names(mult))),
                    name="{}_outputnode_wrapper".format(mult))
                # Connect sub-study outputs
                for output_name in pipeline.multiplicity_output_names(mult):
                    self.workflow.connect(pipeline.outputnode(mult),
                                          output_name,
                                          self._outputnodes[mult],
                                          inv_dataset_map[output_name].name)
            # Copy additional info fields
            self._default_options = copy(pipeline._default_options)
            if override_default_options is not None:
                self._default_options.update(override_default_options)
            self._prereq_options = pipeline._prereq_options
            self._citations = pipeline._citations
            self._description = pipeline._description
