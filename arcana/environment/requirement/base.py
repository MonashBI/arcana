from __future__ import division
from builtins import object
from past.builtins import basestring
import logging
from arcana.exception import (
    ArcanaUsageError, ArcanaVersionNotDectableError,
    ArcanaVersionException)
import re


logger = logging.getLogger('arcana')


class Version(object):
    """
    Representation of a requirement version. Parses version strings that
    follow the convention

    i.e. <MACRO>.<MINOR>[<PRERELEASE>].[<MICRO>[<PRERELEASE>]][.dev<REVISION>]

    Parameters
    ----------
    requirement : Requirement
        The requirement the version is of
    version : str
        The string representation of the version
    """

    delimeter = '.'

    def __init__(self, requirement, version):
        self._req = requirement
        self._seq, self._prerelease, self._dev = self.parse(version)

    @property
    def requirement(self):
        return self._req

    @property
    def sequence(self):
        return self._seq

    def __str__(self):
        s = self.delimeter.join(str(i) for i in self._seq)
        if self._prerelease is not None:
            s += '{}{}'.format(*self._prerelease)
        if self._dev is not None:
            s += '.dev{}'.format(self._dev)
        return s

    def __repr__(self):
        return "{}[{}]".format(self._req, str(self))

    def __eq__(self, other):
        return (self._req == other._req and
                self._seq == other._seq and
                self._prerelease == other._prerelease and
                self._dev == other._dev)

    def compare(self, other):
        """
        Compares the version with another

        Parameters
        ----------
        other : Version
            The version to compare to
        """
        if self._req != other._req:
            raise ArcanaUsageError(
                "Can't compare versions of different requirements {} and {}"
                .format(self._req, other._req))
        # Compare main sequence
        if self._seq < other._seq:
            return -1
        elif self._seq > other._seq:
            return 1
        # If main sequence is equal check prerelease. If a prerelease is
        # None then it is a full release which is > then a prerelease so we
        # just assign it 'z' (which is greater than 'a', 'b' and 'rc')
        s = self._prerelease if self._prerelease is not None else ('z',)
        o = other._prerelease if other._prerelease is not None else ('z',)
        if s < o:
            return -1
        if s > o:
            return 1
        # If both main sequence and prereleases are equal, compare development
        # versions
        if self._dev is not None or other._dev is not None:
            if self._dev is None:
                return -1
            elif other._dev is None:
                return 1
            elif self._dev < other._dev:
                return -1
            elif self._dev > other._dev:
                return 1
        assert self == other
        return 0

    def __lt__(self, other):
        return self.compare(other) < 0

    def __gt__(self, other):
        return self.compare(other) > 0

    def __le__(self, other):
        return self.compare(other) <= 0

    def __ge__(self, other):
        return self.compare(other) >= 0

    @classmethod
    def parse(cls, version_str):
        """
        Splits a typical version string (e.g. <MAJOR>.<MINOR>.<MICRO>)
        into a tuple that can be sorted properly. Ignores all leading
        and trailing characters by using a regex search (instead of match) so
        as to pick the version string out of a block of text.

        Parameters
        ----------
        version_str : str
            The string containing the version numbers

        Returns
        -------
        sequence : tuple(int | str)
            A tuple containing the main sequence of the version,
            e.g. <MAJOR>.<MINOR>.<MICRO>
        prerelease : 2-tuple(str, int) | None
            A 2-tuple containing the type of prerelease ('a' - alpha,
            'b' - beta, or 'rc' - release-canditate) and the number of the
            prerelease
        dev : int | None
            The number of the development version
        """
        # Escape delimeter if required
        m = ('\\' + cls.delimeter if cls.delimeter in r'{}\$.|?*+()[]'
             else cls.delimeter)
        # Pattern to match sub-version
        sub_ver = r'{}\d+[a-zA-Z\-_0-9]*'.format(m)
        regex = r'(?<!\d{m})(\d+{sv}(?:{sv})?(?:{m}\w+)?)'.format(m=m,
                                                                  sv=sub_ver)
        match = re.search(regex, version_str)
        if match is None:
            raise ArcanaVersionNotDectableError(
                "Could not parse version string {} as {}. Regex ({}) did not "
                "match any sub-string".format(version_str, cls.__name__,
                                              regex))
        sequence = []
        prerelease = None
        dev = None
        for part in match.group(1).split(cls.delimeter):
            if part.startswith('dev'):
                try:
                    dev = int(part[3:])
                except ValueError:
                    dev = part[3:]
            else:
                # Split on non-numeric parts of the version string so that we
                # can detect prerelease
                sub_parts = re.split('([^\d]+)', part)
                if sub_parts[0]:
                    try:
                        seq_part = int(sub_parts[0])
                    except ValueError:
                        seq_part = sub_parts[0]
                    sequence.append(seq_part)
                if len(sub_parts) > 1:
                    try:
                        stage, pr_ver = sub_parts[1:]
                    except IndexError:
                        raise ArcanaVersionNotDectableError(
                            "Could not parse version string {} as {}. "
                            "Unrecognised pre-release format of {}"
                            .format(version_str, cls.__name__, part))
                    stage = stage.strip('-_').lower()
                    if 'alpha'.startswith(stage):
                        stage = 'a'
                    elif 'beta'.startswith(stage):
                        stage = 'b'
                    elif stage == 'rc' or stage == 'release-canditate':
                        stage = 'rc'
                    else:
                        raise ArcanaVersionNotDectableError(
                            "Could not parse version string {} as {}. "
                            "Did not recognise pre-release stage {}"
                            .format(version_str, cls.__name__, stage))
                    prerelease = (stage, int(pr_ver))
        return tuple(sequence), prerelease, dev

    def within(self, version):
        """
        A single version can also be interpreted as an open range (i.e. no
        maximum version)
        """
        if isinstance(version, basestring):
            version = type(self._min_ver)(self._req, version)
        return version >= self

    def latest_within(self, *args, **kwargs):
        return self._req.latest_within_range(self, *args, **kwargs)


class Requirement(object):
    """
    Base class for a details of a software package that is required by
    a node of a pipeline.

    Parameters
    ----------
    name : str
        Name of the package
    references : list[Citation]
        A list of references that should be cited when using this software
        requirement
    website : str
        Address of the website detailing the software
    delimeter : str
        Delimeter used to split a version string
    """

    def __init__(self, name, references=None, website=None,
                 version_cls=Version):
        self._name = name.lower()
        self._references = references if references is not None else []
        self._website = website
        self._version_cls = version_cls

    def __eq__(self, other):
        return (self.name == other.name and
                self._references == other._references and
                self.website == other.website and
                self._version_cls == other._version_cls)

    @property
    def name(self):
        return self._name

    @property
    def version_cls(self):
        return self._version_cls

    def __repr__(self):
        return "{}(name={})".format(type(self).__name__, self.name)

    def v(self, version, max_version=None):
        """
        Returns either a single requirement version or a requirement version
        range depending on whether two arguments are supplied or one

        Parameters
        ----------
        version : str | Version
            Either a version of the requirement, or the first version in a
            range of acceptable versions
        """
        if isinstance(version, basestring):
            version = self.version_cls(self, version)
        # Return a version range instead of version
        if max_version is not None:
            if isinstance(max_version, basestring):
                max_version = self.version_cls(self, max_version)
            version = VersionRange(version, max_version)
        return version

    @property
    def references(self):
        return iter(self._references)

    @property
    def website(self):
        return self._website

    def detect_version(self):
        return self.version_cls(self, self.detect_version_str())

    def detect_version_str(self):
        """
        Detects and returns the version string of the software requirement
        that is accessible in the current environment. NB: to be overridden in
        sub-classes.

        * If the requirement is not available in the current environment:
            raise ArcanaRequirementNotFoundError
        * If the requirement is available but its version cannot be detected
          for whatever reason:
            raise ArcanaVersionNotDectableError
        """
        raise NotImplementedError

    def latest_within_range(self, version_range, available,
                            ignore_unrecognised=False):
        """
        Picks the latest acceptible version from the versions available

        Parameters
        ----------
        version_range : VersionRange | Version
            A range of versions or a single version. A single version
            will be interpreted that there are no upper bounds on the version
            range
        available : list(tuple(int) | str)
            List of possible versions to select from
        ignore_unrecognised : bool
            If True, then unrecognisable versions are ignored instead of
            throwing an error

        Returns
        -------
        latest : Version
            The latest version
        """
        latest_ver = None
        for ver in available:
            if isinstance(ver, basestring):
                # Convert to a Version object of matching type
                try:
                    ver = self.version_cls(self, ver)
                except ArcanaVersionNotDectableError:
                    if ignore_unrecognised:
                        logger.warning(
                            "Ignoring unrecognised available version '{}' of "
                            "{}".format(ver, self))
                        continue
                    else:
                        raise
            if version_range.within(ver) and (latest_ver is None or
                                              ver > latest_ver):
                latest_ver = ver
        if latest_ver is None:
            if isinstance(version_range, VersionRange):
                msg_part = 'within range'
            else:
                msg_part = 'greater than'
            raise ArcanaVersionException(
                "Could not find version {} {} from available: {}"
                .format(msg_part, version_range,
                        ', '.join(str(v) for v in available)))
        return latest_ver


class VersionRange(object):
    """
    A range of versions associated with a software requirement

    Parameters
    ----------
    min_version : Version
        The minimum version required by the node
    max_version : Version
        The maximum version that is compatible with the Node
    """

    def __init__(self, min_version, max_version):
        if min_version.requirement != max_version.requirement:
            raise ArcanaUsageError(
                "Inconsistent requirements between min and max versions "
                "({} and {})".format(min_version.requirement,
                                     max_version.requirement))
        self._min_ver = min_version
        self._max_ver = max_version
        if max_version < min_version:
            raise ArcanaUsageError(
                "Maxium version in is less than minimum in {}"
                .format(self))

    @property
    def minimum(self):
        return self._min_ver

    @property
    def maximum(self):
        return self._max_ver

    def __eq__(self, other):
        return (self._min_ver == other._min_ver and
                self._max_ver == other._max_ver)

    def __repr__(self):
        return "{}[{} <= v <= {}]".format(
            self._min_ver.requirement, self.minimum, self.maximum)

    def within(self, version):
        if isinstance(version, basestring):
            version = type(self._min_ver)(self._req, version)
        return version >= self._min_ver and version <= self._max_ver

    def latest_within(self, *args, **kwargs):
        return self._min_ver.requirement.latest_within_range(self, *args,
                                                             **kwargs)
