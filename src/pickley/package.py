import os
import shutil
import sys
import time
import zipfile

import virtualenv

from pickley import ImplementationMap, short, system
from pickley.install import PexRunner, PipRunner
from pickley.pypi import latest_pypi_version, read_entry_points
from pickley.settings import JsonSerializable, SETTINGS
from pickley.uninstall import uninstall_existing


PACKAGERS = ImplementationMap(SETTINGS, "packager")
DELIVERERS = ImplementationMap(SETTINGS, "delivery")


def find_prefix(prefixes, text):
    """
    :param dict prefixes: Prefixes available
    :param str text: Text to examine
    :return str|None: Longest prefix found
    """
    if not text or not prefixes:
        return None
    candidate = None
    for name in prefixes:
        if name and text.startswith(name):
            if not candidate or len(name) > len(candidate):
                candidate = name
    return candidate


def find_site_packages(folder):
    """
    :param str folder: Folder to examine
    :return str|None: Path to lib/site-packages subfolder, if there is one
    """
    if os.path.basename(folder) != "lib":
        folder = os.path.join(folder, "lib")
    if os.path.isdir(folder):
        for name in os.listdir(folder):
            sp = os.path.join(folder, name, "site-packages")
            if os.path.isdir(sp):
                return sp
    return None


def find_entry_points(folder, name, version):
    """
    :param str folder: Folder to examine
    :param str name: Name of pypi package
    :param str version: Version of package
    :return str|None: Path to entry_points.txt file found, if there is one
    """
    if not folder or not name or not version:
        return None
    sp = find_site_packages(folder)
    if not sp:
        return None
    ep = os.path.join(sp, "%s-%s.dist-info" % (name, version), "entry_points.txt")
    if os.path.exists(ep):
        return ep
    if version.endswith(".0"):
        # Try without trailing ".0", as that sometimes gets simplified away by some version parsers
        ep = os.path.join(sp, "%s-%s.dist-info" % (name, version[:-2]), "entry_points.txt")
        if os.path.exists(ep):
            return ep
    # Finally, try also adding ".0", in case something simplified it away before we got 'version'
    ep = os.path.join(sp, "%s-%s.0.dist-info" % (name, version), "entry_points.txt")
    if os.path.exists(ep):
        return ep
    return None


class DeliveryMethod:
    """
    Various implementation of delivering the actual executables
    """

    @classmethod
    def class_implementation_name(cls):
        """
        :return str: Identifier for this delivery type
        """
        return cls.__name__.replace("Delivery", "").lower()

    @property
    def implementation_name(self):
        """
        :return str: Identifier for this packager type
        """
        return self.__class__.class_implementation_name()

    def install(self, packager, target, source):
        """
        :param Packager packager: Associated packager
        :param str target: Full path of executable to deliver (<base>/<entry_point>)
        :param str source: Path to original executable being delivered (.pickley/<package>/...)
        """
        system.delete_file(target)
        if system.DRYRUN:
            system.debug("Would %s %s (source: %s)", self.implementation_name, short(target), short(source))
            return

        if not os.path.exists(source):
            system.error("Can't %s, source %s does not exist", self.implementation_name, short(source))
            return

        system.debug("Delivery: %s %s -> %s", self.implementation_name, short(target), short(source))
        try:
            self._install(packager, target, source)
        except Exception as e:
            system.error("Failed %s %s: %s", self.implementation_name, short(target), e)

    def _install(self, packager, target, source):
        """
        :param Packager packager: Associated packager
        :param str target: Full path of executable to deliver (<base>/<entry_point>)
        :param str source: Path to original executable being delivered (.pickley/<package>/...)
        """
        pass


@DELIVERERS.register
class DeliverySymlink(DeliveryMethod):
    """
    Deliver via symlink
    """

    def _install(self, packager, target, source):
        if os.path.isabs(source) and os.path.isabs(target):
            parent = system.parent_folder(target)
            if system.parent_folder(source).startswith(parent):
                # Use relative path if source is under target
                source = os.path.relpath(source, parent)
        os.symlink(source, target)


@DELIVERERS.register
class DeliveryWrap(DeliveryMethod):
    """
    Deliver via a small wrap that ensures target executable is up-to-date
    """

    def shell(self, *args):
        return system.represented_args(args, shorten=False)

    def _install(self, packager, target, source):
        # Touch the .checked file to avoid an immediate check for upgrades
        checked = SETTINGS.cache.full_path(packager.name, ".checked")
        system.touch(checked)

        pickley = self.shell(SETTINGS.base.full_path(system.PICKLEY))
        checked = self.shell(checked)
        source = self.shell(source)
        with open(target, "wt") as fh:
            fh.write("#!/bin/bash\n\n")
            fh.write("# Wrapper generated by https://pypi.org/project/pickley/\n\n")
            fh.write("if [[ -x %s ]]; then\n" % pickley)
            fh.write("  if [[ ! -f %s || -n `find %s -mmin +60 2> /dev/null` ]]; then\n" % (checked, checked))
            fh.write("    touch %s\n" % checked)
            fh.write("    %s --quiet install %s\n" % (pickley, packager.name))
            fh.write("    exec %s $*\n" % self.shell(target))
            fh.write("  fi\n")
            fh.write("fi\n\n")
            fh.write("exec %s $*\n" % source)
        system.make_executable(target)


@DELIVERERS.register
class DeliveryCopy(DeliveryMethod):
    """
    Deliver by copy
    """

    def _install(self, packager, target, source):
        system.ensure_folder(target)
        if os.path.isdir(source):
            shutil.copytree(source, target, symlinks=False)
        else:
            shutil.copy(source, target)
        shutil.copystat(source, target)  # Make sure last modification time is preserved


class VersionMeta(JsonSerializable):
    """
    Version meta on a given package
    """

    _latest_validity = 30 * 60      # type: int # How long in seconds to consider determined latest version valid for
    _problem = None                 # type: str # Detected problem, if any
    _name = None                    # type: str # Associated pypi package name
    channel = ""                    # type: str # Channel (stable, latest, ...) via which this version was determined
    packager = ""                   # type: str # Packager used
    source = ""                     # type: str # Description of where definition came from
    timestamp = None                # type: float # Epoch when version was determined (useful to cache "expensive" calls to pypi)
    version = ""                    # type: str # Effective version

    def __init__(self, name, suffix=None):
        """
        :param str name: Associated pypi package name
        :param str|None suffix: Optional suffix where to store this object
        """
        self._name = name
        if suffix:
            self._path = SETTINGS.cache.full_path(self.name, ".%s.json" % suffix)

    def __repr__(self):
        return self.representation()

    def representation(self, verbose=False, note=None):
        """
        :param bool verbose: If True, show more extensive info
        :return str: Human readable representation
        """
        if self._problem:
            lead = "%s: %s" % (self.name, self._problem)
        elif self.version:
            lead = "%s %s" % (self.name, self.version)
        else:
            lead = "%s: no version" % (self.name)
        notice = ""
        if verbose:
            notice = []
            if self.packager:
                notice.append("as %s" % self.packager)
            if self.channel:
                notice.append("channel: %s" % self.channel)
            if self.source and self.source != SETTINGS.index:
                notice.append("source: %s" % self.source)
            if notice:
                notice = " (%s)" % ", ".join(notice)
            else:
                notice = ""
        if note:
            notice = " %s%s" % (note, notice)
        return "%s%s" % (lead, notice)

    @property
    def name(self):
        """
        :return str: Associated pypi package name
        """
        return self._name

    @property
    def problem(self):
        """
        :return str|None: Problem description, if any
        """
        return self._problem

    @property
    def valid(self):
        """
        :return bool: Was version determined successfully?
        """
        return bool(self.version) and not self._problem

    @property
    def file_exists(self):
        """
        :return bool: True if corresponding json file exists
        """
        return self._path and os.path.exists(self._path)

    def equivalent(self, other):
        """
        :param VersionMeta other: VersionMeta to compare to
        :return bool: True if 'self' is equivalent to 'other'
        """
        if other is None:
            return False
        if self.version != other.version:
            return False
        if self.packager != other.packager:
            return False
        return True

    def set_version(self, version, source, channel="", packager=""):
        """
        :param str version: Effective version
        :param str source: Description of where version determination came from
        :param str channel: Channel (stable, latest, ...) via which this version was determined
        :param str packager: Packager (pex, venv, ...) used
        """
        self.version = version
        self.source = source
        self.channel = channel
        self.packager = packager
        self.timestamp = time.time()

    def set(self, other):
        """
        :param VersionMeta other:
        """
        self._problem = other._problem
        self.channel = other.channel
        if other.packager:
            self.packager = other.packager
        self.source = other.source
        self.timestamp = other.timestamp
        self.version = other.version

    def invalidate(self, problem):
        """
        :param str problem: Description of problem
        """
        self._problem = problem
        self.version = ""

    @property
    def still_valid(self):
        """
        :return bool: Is this version determination still valid? (based on timestamp)
        """
        if not self.valid or not self.timestamp:
            return self.valid
        try:
            return (time.time() - self.timestamp) < self._latest_validity
        except Exception:
            return False


class Packager(object):
    """
    Interface of a packager
    """

    def __init__(self, name, cache=None):
        """
        :param str name: Name of pypi package
        :param str|None cache: Optional custom cache folder to use
        """
        self.name = name
        self.cache = system.resolved_path(cache) or SETTINGS.cache.full_path(self.name, "dist")
        self._entry_points = None
        self.current = VersionMeta(self.name, "current")
        self.latest = VersionMeta(self.name, "latest")
        self.desired = VersionMeta(self.name)
        self.dist_folder = SETTINGS.cache.full_path(self.name)
        self.source_folder = None

    def __repr__(self):
        return "%s %s" % (self.implementation_name, self.name)

    @classmethod
    def class_implementation_name(cls):
        """
        :return str: Identifier for this packager type
        """
        return cls.__name__.replace("Packager", "").lower()

    @property
    def implementation_name(self):
        """
        :return str: Identifier for this packager type
        """
        return self.__class__.class_implementation_name()

    @property
    def entry_points_path(self):
        return SETTINGS.cache.full_path(self.name, ".entry-points.json")

    @property
    def entry_points(self):
        """
        :return list|None: Determined entry points from produced wheel, if available
        """
        if self._entry_points is None:
            self._entry_points = JsonSerializable.get_json(self.entry_points_path)
            if self._entry_points is None:
                self._entry_points = [self.name] if system.DRYRUN else []
        return self._entry_points

    def set_dist_folder(self, dist_folder):
        """
        :param str|None dist_folder: Set 'dist_folder' (where packages end up being delivered)
        """
        if dist_folder:
            self.dist_folder = system.resolved_path(dist_folder)

    def set_source_folder(self, source_folder):
        """
        :param str|None source_folder: Set 'source_folder' (for local packaging, from not yet released checkout)
        """
        self.source_folder = system.resolved_path(source_folder)

    def refresh_entry_points(self, folder, version):
        """
        :param str folder: Folder where to look for entry points
        :param str version: Version of package
        """
        if system.DRYRUN:
            return
        self._entry_points = self.get_entry_points(folder, version)
        if not self._entry_points:
            system.abort("'%s' is not a CLI, it has no console_scripts entry points", self.name)
        JsonSerializable.save_json(self._entry_points, self.entry_points_path)

    def get_entry_points(self, folder, version):
        """
        :param str folder: Folder where to look for entry points
        :param str version: Version of package
        :return list|None: Determine entry points for pypi package with 'self.name'
        """
        system.abort("get_entry_points not implemented for %s", self.implementation_name)

    def cleanup(self):
        """Delete build cache and older installs"""
        system.delete_file(self.cache)

        # Scan installation folder, looking for previous installs
        folder = SETTINGS.cache.full_path(self.name)
        prefixes = {None: [], self.name: []}
        for name in self.entry_points:
            prefixes[name] = []
        if os.path.isdir(folder):
            for name in os.listdir(folder):
                if name.startswith("."):
                    continue
                target = find_prefix(prefixes, name)
                if target in prefixes:
                    fpath = os.path.join(folder, name)
                    prefixes[target].append((os.path.getmtime(fpath), fpath))

        # Cleanup all but the latest
        for _, cleanable in prefixes.items():
            cleanable = sorted(cleanable)[:-1]
            for _, path in cleanable:
                system.delete_file(path)

    def refresh_current(self):
        """Refresh self.current"""
        self.current.load()
        if not self.current.valid:
            self.current.invalidate("is not installed")

    def refresh_latest(self):
        """Refresh self.latest"""
        self.latest.load()
        if self.latest.still_valid:
            return

        version = latest_pypi_version(SETTINGS.index, self.name)
        self.latest.set_version(version, SETTINGS.index or "pypi", channel="latest")
        if version:
            self.latest.save()

        else:
            self.latest.invalidate("can't determine latest version")

    def refresh_desired(self):
        """Refresh self.desired"""
        configured = SETTINGS.version(self.name)
        if configured.value:
            self.desired.set_version(
                configured.value, str(configured.source), channel=configured.channel, packager=self.implementation_name
            )
            return
        if configured.channel == "latest":
            self.refresh_latest()
            self.desired.set(self.latest)
            self.desired.packager = self.implementation_name
            return
        self.desired.invalidate("can't determine %s version" % configured.channel)

    def install(self, force=False, bootstrap=False):
        """
        :param bool force: If True, re-install even if package is already installed
        :param bool bootstrap: Bootstrap mode
        """
        if not bootstrap:
            self.refresh_current()
            self.refresh_desired()
            if not self.desired.valid:
                system.abort("Can't install %s: %s", self.name, self.desired.problem)
            if not force and self.current.equivalent(self.desired):
                system.info(self.desired.representation(verbose=True, note="is already installed"))
                return

        self.effective_install(self.desired.version)

        self.current.set(self.desired)
        self.current.save()
        msg = "bootstrap" if bootstrap else "install"
        msg = "Would %s" % msg if system.DRYRUN else "%sed" % (msg.title())
        system.info("%s %s", msg, self.desired.representation(verbose=True))

    def effective_install(self, version):
        """
        :param str version: Effective version to install
        """
        system.abort("Not implemented")

    def perform_delivery(self, version, source):
        """
        :param str version: Version being delivered
        :param str source: Template describing where source is coming from, example: {cache}/{name}-{version}
        """
        deliverer_definition = DELIVERERS.resolved(self.name)
        if not deliverer_definition:
            system.abort("No delivery type configured for %s", self.name)

        deliverer = DELIVERERS.get(deliverer_definition.value)
        if not deliverer:
            system.abort("Unknown delivery type '%s'", deliverer_definition)

        for name in self.entry_points:
            target = SETTINGS.base.full_path(name)
            if self.name != system.PICKLEY and not self.current.file_exists:
                uninstall_existing(target)
            if name != self.name:
                # Delete any previously present delivery
                system.delete_file(SETTINGS.cache.full_path(self.name, "%s-%s" % (name, version)))
            path = source.format(cache=SETTINGS.cache.full_path(self.name), name=name, version=version)
            deliverer().install(self, target, path)


class WheelBasedPackager(Packager):
    """
    Common implementation for wheel-based packagers
    """

    def __init__(self, name, cache=None):
        super(WheelBasedPackager, self).__init__(name, cache=cache)
        self.pip = PipRunner(self.cache)

    def get_entry_points(self, folder, version):
        """
        :param str folder: Folder where to look for entry points
        :param str version: Version of package
        :return list|None: Determine entry points for pypi package with 'self.name'
        """
        if not os.path.isdir(self.pip.cache):
            return None
        prefix = "%s-%s-" % (self.name, version)
        for fname in os.listdir(self.pip.cache):
            if fname.startswith(prefix) and fname.endswith(".whl"):
                wheel_path = os.path.join(self.pip.cache, fname)
                try:
                    with zipfile.ZipFile(wheel_path, "r") as wheel:
                        for fname in wheel.namelist():
                            if os.path.basename(fname) == "entry_points.txt":
                                with wheel.open(fname) as fh:
                                    return read_entry_points(fh)
                except Exception as e:
                    system.error("Can't read wheel %s: %s", wheel_path, e, exc_info=e)
        return None


@PACKAGERS.register
class PexPackager(WheelBasedPackager):
    """
    Package/install via pex (https://pypi.org/project/pex/)
    """

    def __init__(self, name, cache=None):
        """
        :param str name: Name of pypi package
        :param str|None cache: Optional path to folder to use as build cache
        """
        super(PexPackager, self).__init__(name, cache=cache)
        self.pex = PexRunner(self.cache)

    def package(self, version=None):
        """
        :param str|None version: If provided, append version as suffix to produced pex
        :return list|None: List of produced packages (files), if successful
        """
        if not version and not self.source_folder:
            system.abort("Need either source_folder or version in order to package")

        if not version:
            setup_py = os.path.join(self.source_folder, "setup.py")
            if not os.path.isfile(setup_py):
                system.abort("No setup.py in %s", short(self.source_folder))
            version = system.run_program(sys.executable, setup_py, "--version", fatal=False)
            if not version:
                system.abort("Could not determine version from %s", short(setup_py))

        error = self.pip.wheel(self.source_folder if self.source_folder else "%s==%s" % (self.name, version))
        if error:
            system.abort("pip wheel failed: %s", error)

        self.refresh_entry_points(self.pip.cache, version)
        result = []
        system.ensure_folder(self.dist_folder, folder=True)
        for name in self.entry_points:
            dest = name if self.source_folder else "%s-%s" % (name, version)
            dest = os.path.join(self.dist_folder, dest)

            error = self.pex.build(name, self.name, version, dest)
            if error:
                system.abort("pex command failed: %s", error)
            result.append(dest)

        return result

    def effective_install(self, version):
        """
        :param str version: Effective version to install
        """
        # Delete any previously present venv
        system.delete_file(SETTINGS.cache.full_path(self.name, "%s-%s" % (self.name, version)))

        self.package(version=version)
        self.perform_delivery(version, "{cache}/{name}-{version}")


@PACKAGERS.register
class VenvPackager(Packager):
    """
    Install via virtualenv (https://pypi.org/project/virtualenv/)
    """

    def get_entry_points(self, folder, version):
        """
        :param str folder: Folder where to look for entry points
        :param str version: Version of package
        :return list|None: Determine entry points for pypi package with 'self.name'
        """
        ep = find_entry_points(folder, self.name, version)
        if ep:
            with open(ep, "rt") as fh:
                return read_entry_points(fh)
        return None

    def is_within(self, venv, working_folder):
        """
        :return bool: True if 'venv' path is under 'working_folder'
        """
        return venv.lower().startswith(working_folder.lower())

    def effective_install(self, version):
        """
        :param str version: Effective version to install
        """
        venv = virtualenv.__file__
        if not venv:
            system.abort("Can't determine path to virtualenv.py")

        if venv.endswith(".pyc"):
            venv = venv[:-1]

        install_folder = SETTINGS.cache.full_path(self.name, "%s-%s" % (self.name, version))
        working_folder = install_folder
        if self.is_within(venv, working_folder):
            # Create venv in a different folder, to support the bootstrap case
            working_folder = "%s.tmp" % install_folder

        python = SETTINGS.resolved_value("python", package_name=self.name)
        pip = os.path.join(working_folder, "bin", "pip")
        system.run_program(system.PYTHON, venv, working_folder, "-p", python)
        system.run_program(pip, "--disable-pip-version-check", "install", "%s==%s" % (self.name, version), "-i", SETTINGS.index)

        if install_folder != working_folder:
            system.run_program(system.PYTHON, venv, "--relocatable", working_folder, "-p", python)
            system.delete_file(install_folder)
            if system.DRYRUN:
                system.debug("Would move %s -> %s", short(working_folder), short(install_folder))
            else:
                system.debug("Moving %s -> %s", short(working_folder), short(install_folder))
                shutil.move(working_folder, install_folder)

        self.refresh_entry_points(install_folder, version)
        self.perform_delivery(version, "%s/{name}" % os.path.join(install_folder, "bin"))
