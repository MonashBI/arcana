"""
Arcana

Copyright (c) 2012-2018 Thomas G. Close, Monash Biomedical Imaging,
Monash University, Melbourne, Australia

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
"""

from .__about__ import __version__, __authors__
import os
from .analysis import (
    Analysis, AnalysisMetaClass, MultiAnalysis, MultiAnalysisMetaClass,
    SubCompSpec, Parameter, ParamSpec, SwitchSpec)
from .data import (
    FilesetSpec, FilesetFilter, FieldSpec, FieldFilter,
    InputFilesetSpec, InputFieldSpec, OutputFilesetSpec, OutputFieldSpec)
from .data.file_format import FileFormat
from .data import Fileset, Field, FilesetSlice, FieldSlice
from .processor import (
    SingleProc, MultiProc, SlurmProc)
from .environment import StaticEnv, ModulesEnv
from .repository import XnatRepo, Dataset
from .repository.local import LocalFileSystemRepo
# Should be set explicitly in all FSL interfaces, but this squashes the warning
os.environ['FSLOUTPUTTYPE'] = 'NIFTI_GZ'
