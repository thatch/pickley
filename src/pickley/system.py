"""
Functionality for the whole app, easily importable via one name
"""

import io
import logging
import os
import re
import shutil
import subprocess  # nosec
import sys
import time
from logging.handlers import RotatingFileHandler

import six

from pickley import decode, pickley_program_path


LOG = logging.getLogger(__name__)
PICKLEY = "pickley"

HOME = os.path.expanduser("~")
DRYRUN = False
WRAPPER_MARK = "# Wrapper generated by https://pypi.org/project/pickley/"

SETTINGS = None  # type: pickley.settings.Settings
DESIRED_PYTHON = None  # type: str # Desired python installation (if explicitly stated via CLI flags)

LATEST_CHANNEL = "latest"
VENV_PACKAGER = "venv"
DEFAULT_DELIVERY = "symlink"
INVOKER = "invoker"

RE_PYTHON_LOOSE = re.compile(r"(py(thon ?)?)?([0-9])?\.?([0-9])?\.?[0-9]*", re.IGNORECASE)
RE_PYTHON_STRICT = re.compile(r"(python([0-9]\.[0-9])|([0-9]\.[0-9])\.?[0-9]*)")


class State:
    """Helps track state without using globals"""

    output = True  # print() warning/error messages (turned off when we do have a logger to console)
    testing = False  # print all messages, useful when running tests
    logging = False  # If false, no loggers have been setup, so no point in logging

    # Log handlers, allows to setup logging once
    audit_handler = None
    debug_handler = None


def resolved_path(path, base=None):
    """
    :param str path: Path to resolve
    :param str|None base: Base path to use to resolve relative paths (default: current working dir)
    :return str: Absolute path
    """
    if not path:
        return path
    path = os.path.expanduser(path)
    if base and not os.path.isabs(path):
        return os.path.join(base, path)
    return os.path.abspath(path)


PICKLEY_PROGRAM_PATH = resolved_path(pickley_program_path())
sys.argv[0] = PICKLEY_PROGRAM_PATH


def short(path, shorten=None, meta=True):
    """
    :param path: Path to represent in its short form
    :param str|None shorten: Extra folder to relativise paths to
    :param bool meta: If True, shorten paths relatively to SYSTEM.meta as well
    :return str: Short form, using '~' if applicable
    """
    if not path:
        return path
    path = str(path)
    anchors = []
    if meta and SETTINGS:
        anchors.append(SETTINGS.meta.path)
    if shorten:
        anchors.append(shorten)
    if anchors:
        for p in sorted(anchors, reverse=True):
            path = path.replace(p + "/", "")
    path = path.replace(HOME, "~")
    return path


def debug(message, *args, **kwargs):
    if State.logging:
        LOG.debug(message, *args, **kwargs)
    if State.testing:
        print(str(message) % args)


def info(message, *args, **kwargs):
    output = kwargs.pop("output", State.output)
    if State.logging:
        LOG.info(message, *args, **kwargs)
    if output or State.testing:
        print(str(message) % args)


def warning(message, *args, **kwargs):
    if State.logging:
        LOG.warning(message, *args, **kwargs)
    if State.output or State.testing:
        print("WARNING: %s" % (str(message) % args))


def error(message, *args, **kwargs):
    if State.logging:
        LOG.error(message, *args, **kwargs)
    if State.output or State.testing:
        print("ERROR: %s" % (str(message) % args))


def abort(*args, **kwargs):
    """
    :param args: Args passed through for error reporting
    :param kwargs: Args passed through for error reporting
    :return: kwargs["return_value"] (default: -1) to signify failure to non-fatal callers
    """
    code = kwargs.pop("code", 1)
    fatal = kwargs.pop("fatal", True)
    quiet = kwargs.pop("quiet", False)
    return_value = kwargs.pop("return_value", -1)
    if not quiet and args:
        if code == 0:
            info(*args, **kwargs)
        else:
            error(*args, **kwargs)
    if fatal:
        sys.exit(code)
    return return_value


def despecced(text):
    """
    :param str text: Text of form <name>==<version>, or just <name>
    :return str, str|None: Name and version
    """
    spec = None
    if "==" in text:
        i = text.strip().index("==")
        spec = text[i + 2:]
        text = text[:i]
    return text, spec


def installed_names():
    """Yield names of currently installed packages"""
    result = []
    if os.path.isdir(SETTINGS.meta.path):
        for fname in os.listdir(SETTINGS.meta.path):
            fpath = os.path.join(SETTINGS.meta.path, fname)
            if os.path.isdir(fpath):
                if os.path.exists(os.path.join(fpath, ".current.json")):
                    result.append(fname)
    return result


def setup_audit_log():
    """Log to <meta>/audit.log"""
    if DRYRUN or State.audit_handler:
        return
    path = SETTINGS.meta.full_path("audit.log")
    ensure_folder(path)
    State.audit_handler = RotatingFileHandler(path, maxBytes=500 * 1024, backupCount=1)
    State.audit_handler.setLevel(logging.DEBUG)
    State.audit_handler.setFormatter(logging.Formatter("%(asctime)s [%(process)s] %(levelname)s - %(message)s"))
    logging.root.addHandler(State.audit_handler)
    State.logging = True
    info(":: %s", represented_args(sys.argv), output=False)


def setup_debug_log():
    """Log to stderr"""
    # Log to console with --debug or --dryrun
    if State.debug_handler:
        return
    State.output = False
    State.debug_handler = logging.StreamHandler()
    State.debug_handler.setLevel(logging.DEBUG)
    State.debug_handler.setFormatter(logging.Formatter("%(levelname)s - %(message)s"))
    logging.root.addHandler(State.debug_handler)
    logging.root.setLevel(logging.DEBUG)
    State.logging = True


def get_lines(path, max_size=8192, fatal=True, quiet=False):
    """
    :param str path: Path of text file to return lines from
    :param int max_size: Return contents only for files smaller than 'max_size' bytes
    :param bool fatal: Abort execution on failure if True
    :param bool quiet: Don't log if True
    :return list|None: Lines from file contents
    """
    if not path or not os.path.isfile(path) or os.path.getsize(path) > max_size:
        # Intended for small text files, pretend no contents for binaries
        return None

    try:
        with io.open(path, "rt", errors="ignore") as fh:
            return fh.readlines()

    except Exception as e:
        return abort("Can't read %s: %s", short(path), e, fatal=fatal, quiet=quiet, return_value=None)


def virtualenv_path():
    """
    :return str: Path to our own virtualenv.py
    """
    import virtualenv
    path = virtualenv.__file__
    if path and path.endswith(".pyc"):
        path = path[:-1]
    return path


def is_universal(wheels_folder, package_name, version):
    """
    :param str wheels_folder: Path to folder where wheels reside
    :param str package_name: Pypi package name
    :param str version: Specific version of 'package_name' to examine
    :return bool: True if wheel exists and is universal
    """
    if os.path.isdir(wheels_folder):
        prefix = "%s-%s-" % (package_name, version)
        for fname in os.listdir(wheels_folder):
            if fname.startswith(prefix) and fname.endswith(".whl"):
                return "py2.py3-none" in fname


def added_env_paths(env_vars, env=None):
    """
    :param dict env_vars: Env vars to customize
    :param dict env: Original env vars
    """
    if not env_vars:
        return None
    if not env:
        env = dict(os.environ)
    result = dict(env)
    for env_var, paths in env_vars.items():
        separator = paths[0]
        paths = paths[1:]
        current = env.get(env_var, "")
        current = [x for x in current.split(separator) if x]
        added = 0
        for path in paths.split(separator):
            if path not in current:
                added += 1
                current.append(path)
        if added:
            result[env_var] = separator.join(current)
    return result


def file_younger(path, age):
    """
    :param str path: Path to file
    :param int age: How many seconds to consider the file too old
    :return bool: True if file exists and is younger than 'age' seconds
    """
    try:
        return time.time() - os.path.getmtime(path) < age

    except OSError:
        return False


def check_pid(pid):
    """Check For the existence of a unix pid"""
    try:
        os.kill(pid, 0)
        return True
    except (OSError, TypeError):
        return False


def touch(path):
    """
    :param str path: Path to file to touch
    """
    return write_contents(path, "")


def to_unicode(s):
    """Helps deal with py2/3 differences around unicode"""
    if isinstance(s, six.text_type):
        return s
    return six.text_type(s)


def write_contents(path, contents, verbose=False, fatal=True):
    """
    :param str path: Path to file
    :param str contents: Contents to write
    :param bool verbose: Don't log if False (dryrun being always logged)
    :param bool fatal: Abort execution on failure if True
    :return int: 1 if effectively done, 0 if no-op, -1 on failure
    """
    if not path:
        return 0

    if DRYRUN:
        action = "write %s bytes to" % len(contents) if contents else "touch"
        debug("Would %s %s", action, short(path))
        return 1

    ensure_folder(path, fatal=fatal)
    if verbose and contents:
        debug("Writing %s bytes to %s", len(contents), short(path))

    try:
        with io.open(path, "wt") as fh:
            if contents:
                fh.write(to_unicode(contents))
            else:
                os.utime(path, None)
        return 1

    except Exception as e:
        return abort("Can't write to %s: %s", short(path), e, fatal=fatal)


def parent_folder(path, base=None):
    """
    :param str path: Path to file or folder
    :param str|None base: Base folder to use for relative paths (default: current working dir)
    :return str: Absolute path of parent folder of 'path'
    """
    return path and os.path.dirname(resolved_path(path, base=base))


def first_line(path):
    """
    :param str path: Path to file
    :return str|None: First line of file, if any
    """
    try:
        with io.open(path, "rt", errors="ignore") as fh:
            return fh.readline().strip()
    except (IOError, TypeError):
        return None


def flatten(result, value, separator=None, unique=True):
    """
    :param list result: Flattened values
    :param value: Possibly nested arguments (sequence of lists, nested lists)
    :param str|None separator: Split values with 'separator' if specified
    :param bool unique: If True, return unique values only
    """
    if not value:
        # Convenience: allow to filter out --foo None easily
        if value is None and not unique and result and result[-1].startswith("-"):
            result.pop(-1)
        return
    if isinstance(value, (list, tuple, set)):
        for item in value:
            flatten(result, item, separator=separator, unique=unique)
        return
    if separator is not None and hasattr(value, "split") and separator in value:
        flatten(result, value.split(separator), separator=separator, unique=unique)
        return
    if not unique or value not in result:
        result.append(value)


def flattened(value, separator=None, unique=True):
    """
    :param value: Possibly nested arguments (sequence of lists, nested lists)
    :param str|None separator: Split values with 'separator' if specified
    :param bool unique: If True, return unique values only
    :return list: 'value' flattened out (leaves from all involved lists/tuples)
    """
    result = []
    flatten(result, value, separator=separator, unique=unique)
    return result


def relocate_venv_file(path, source, destination, fatal=True, quiet=False):
    """
    :param str path: Path of file to relocate (change mentions of 'source' to 'destination')
    :param str source: Where venv used to be
    :param str destination: Where venv is moved to
    :param bool fatal: Abort execution on failure if True
    :param bool quiet: Don't log if True
    :return int: 1 if effectively done, 0 if no-op, -1 on failure
    """
    content = get_lines(path, fatal=fatal, quiet=quiet)
    if not content:
        return 0

    modified = False
    lines = []
    for line in content:
        if source in line:
            line = line.replace(source, destination)
            modified = True
        lines.append(line)

    if not modified:
        return 0

    return write_contents(path, "".join(lines), fatal=fatal)


def ensure_folder(path, folder=False, fatal=True):
    """
    :param str path: Path to file or folder
    :param bool folder: If True, 'path' refers to a folder (file otherwise)
    :param bool fatal: Abort execution on failure if True
    :return int: 1 if effectively done, 0 if no-op, -1 on failure
    """
    if not path:
        return 0

    if folder:
        folder = resolved_path(path)
    else:
        folder = parent_folder(path)
    if os.path.isdir(folder):
        return 0

    if DRYRUN:
        debug("Would create %s", short(folder))
        return 1

    try:
        os.makedirs(folder)
        return 1

    except Exception as e:
        return abort("Can't create folder %s: %s", short(folder), e, fatal=fatal)


def copy_file(source, destination, fatal=True):
    """
    Copy source -> destination

    :param str source: Source file or folder
    :param str destination: Destination file or folder
    :param bool fatal: Abort execution on failure if True
    :return int: 1 if effectively done, 0 if no-op, -1 on failure
    """
    return _with_relocation(source, destination, _copy, fatal)


def move_file(source, destination, fatal=True):
    """
    Move source -> destination

    :param str source: Source file or folder
    :param str destination: Destination file or folder
    :param bool fatal: Abort execution on failure if True
    :return int: 1 if effectively done, 0 if no-op, -1 on failure
    """
    return _with_relocation(source, destination, _move, fatal)


def _with_relocation(source, destination, func, fatal):
    """
    Call func(source, destination)

    :param str source: Source file or folder
    :param str destination: Destination file or folder
    :param callable func: Implementation function
    :param bool fatal: Abort execution on failure if True
    :return int: 1 if effectively done, 0 if no-op, -1 on failure
    """
    if not source or not destination or source == destination:
        return 0

    action = func.__name__[1:]
    psource = parent_folder(source)
    pdest = resolved_path(destination)
    if psource != pdest and psource.startswith(pdest):
        return abort("Can't %s %s -> %s: source contained in destination", action, short(source), short(destination), fatal=fatal)

    if DRYRUN:
        debug("Would %s %s -> %s", action, short(source), short(destination))
        return 1

    if not os.path.exists(source):
        return abort("%s does not exist, can't %s to %s", short(source), action.title(), short(destination), fatal=fatal)

    relocated = 0
    for bin_folder in find_venvs(source):
        for name in os.listdir(bin_folder):
            fpath = os.path.join(bin_folder, name)
            relocated += relocate_venv_file(fpath, source, destination, fatal=fatal)

    info = " (relocated %s)" % relocated if relocated else ""
    debug("%s %s -> %s%s", action.title(), short(source), short(destination), info)

    ensure_folder(destination, fatal=fatal)
    delete_file(destination, fatal=fatal, quiet=True)
    try:
        func(source, destination)
        return 1

    except Exception as e:
        return abort("Can't %s %s -> %s: %s", action, short(source), short(destination), e, fatal=fatal)


def _copy(source, destination):
    """Effective copy"""
    if os.path.isdir(source):
        shutil.copytree(source, destination, symlinks=True)
    else:
        shutil.copy(source, destination)

    shutil.copystat(source, destination)  # Make sure last modification time is preserved


def _move(source, destination):
    """Effective move"""
    shutil.move(source, destination)


def find_venvs(folder, seen=None):
    """
    :param str folder: Folder to scan for venvs
    :param dict|None seen: Allows to not get stuck on circular symlinks
    """
    if folder and os.path.isdir(folder):
        if seen is None:
            folder = os.path.realpath(folder)
            seen = set()

        if folder not in seen:
            seen.add(folder)
            files = os.listdir(folder)
            if "bin" in files:
                bin_folder = os.path.join(folder, "bin")
                if is_executable(os.path.join(bin_folder, "python")):
                    yield bin_folder
                    return
            for name in files:
                fname = os.path.join(folder, name)
                for path in find_venvs(fname, seen=seen):
                    yield path


def delete_file(path, fatal=True, quiet=False):
    """
    :param str|None path: Path to file or folder to delete
    :param bool fatal: Abort execution on failure if True
    :param bool quiet: Don't log if True
    :return int: 1 if effectively done, 0 if no-op, -1 on failure
    """
    islink = path and os.path.islink(path)
    if not islink and (not path or not os.path.exists(path)):
        return 0

    if DRYRUN:
        debug("Would delete %s", short(path))
        return 1

    if not quiet:
        debug("Deleting %s", short(path))
    try:
        if islink or os.path.isfile(path):
            os.unlink(path)
        else:
            shutil.rmtree(path)
        return 1

    except Exception as e:
        return abort("Can't delete %s: %s", short(path), e, fatal=fatal)


def make_executable(path, fatal=True):
    """
    :param str path: chmod file with 'path' as executable
    :param bool fatal: Abort execution on failure if True
    :return int: 1 if effectively done, 0 if no-op, -1 on failure
    """
    if is_executable(path):
        return 0

    if DRYRUN:
        debug("Would make %s executable", short(path))
        return 1

    if not os.path.exists(path):
        return abort("%s does not exist, can't make it executable", short(path), fatal=fatal)

    try:
        os.chmod(path, 0o755)  # nosec
        return 1

    except Exception as e:
        return abort("Can't chmod %s: %s", short(path), e, fatal=fatal)


def is_executable(path):
    """
    :param str path: Path to file
    :return bool: True if file exists and is executable
    """
    return path and os.path.isfile(path) and os.access(path, os.X_OK)


def which(program, ignore_own_venv=False):
    """
    :param str program: Program name to find via env var PATH
    :param bool ignore_own_venv: If True, do not resolve to executables in current venv
    :return str|None: Full path to program, if one exists and is executable
    """
    if not program:
        return None
    if os.path.isabs(program):
        return program if is_executable(program) else None
    for p in os.environ.get("PATH", "").split(":"):
        fp = os.path.join(p, program)
        if (not ignore_own_venv or not fp.startswith(sys.prefix)) and is_executable(fp):
            return fp
    return None


def run_python(*args, **kwargs):
    """Invoke targetted python interpreter with given args"""
    package_name = kwargs.pop("package_name", None)
    python = target_python(package_name=package_name)
    return run_program(python.executable, *args, **kwargs)


def run_program(program, *args, **kwargs):
    """Run 'program' with 'args'"""
    args = flattened(args, unique=False)
    full_path = which(program)

    fatal = kwargs.pop("fatal", True)
    dryrun = kwargs.pop("dryrun", fatal and DRYRUN)
    include_error = kwargs.pop("include_error", False)
    quiet = kwargs.pop("quiet", False)
    shorten = kwargs.pop("shorten", None)

    message = "Would run" if dryrun else "Running"
    message = "%s: %s %s" % (message, short(full_path or program, shorten=shorten), represented_args(args, shorten=shorten))
    if not quiet:
        logger = kwargs.pop("logger", debug)
        logger(message)

    if dryrun:
        return message

    if not full_path:
        return abort("%s is not installed", short(program, shorten=shorten), fatal=fatal, quiet=quiet, return_value=None)

    stdout = kwargs.pop("stdout", subprocess.PIPE)
    stderr = kwargs.pop("stderr", subprocess.PIPE)
    args = [full_path] + args
    try:
        p = subprocess.Popen(args, stdout=stdout, stderr=stderr, env=added_env_paths(kwargs.pop("path_env", None)))  # nosec
        output, error = p.communicate()
        output = decode(output)
        error = decode(error)
        if output is not None:
            output = output.strip()
        if error is not None:
            error = error.strip()

        if p.returncode:
            info = ": %s\n%s" % (error, output) if output or error else ""
            message = "%s exited with code %s%s" % (short(program, shorten=shorten), p.returncode, info.strip())
            return abort(message, fatal=fatal, quiet=quiet, return_value=None)

        if include_error and error:
            output = "%s\n%s" % (output, error)
        return output and output.strip()

    except Exception as e:
        return abort("%s failed: %s", short(program, shorten=shorten), e, exc_info=e, fatal=fatal, quiet=quiet, return_value=None)


def quoted(text):
    """
    :param str text: Text to optionally quote
    :return str: Quoted if 'text' contains spaces
    """
    if text and " " in text:
        sep = "'" if '"' in text else '"'
        return "%s%s%s" % (sep, text, sep)
    return text


def represented_args(args, shorten=None, separator=" "):
    """
    :param list|tuple args: Arguments to represent
    :param str|None shorten: Extra folder to relativise paths to
    :param str separator: Separator to use
    :return str: Quoted as needed textual representation
    """
    result = []
    for text in args:
        result.append(quoted(short(text, shorten=shorten)))
    return separator.join(result)


def to_int(text, default=None):
    """
    :param str|int|float text: Value to convert
    :param int|float|None default: Default to use if 'text' can't be parsed
    :return float:
    """
    try:
        return int(text)
    except (TypeError, ValueError):
        return default


class FolderBase(object):
    """
    This class allows to more easily deal with folders
    """

    def __init__(self, path, name=None):
        """
        :param str path: Path to folder
        :param str|None name: Name of this folder (defaults to basename of 'path')
        """
        self.path = resolved_path(path)
        self.name = name or os.path.basename(path)

    def relative_path(self, path):
        """
        :param str path: Path to relativize
        :return str: 'path' relative to self.path
        """
        return os.path.relpath(path, self.path)

    def relativize(self, component):
        return component if not component or not component.startswith("/") else component[1:]

    def full_path(self, *relative):
        """
        :param list(str) *relative: Relative components
        :return str: Full path based on self.path
        """
        relative = [self.relativize(c) for c in relative]
        return os.path.join(self.path, *relative)

    def __repr__(self):
        return "%s: %s" % (self.name, short(self.path))


def parent_python():
    prefix = getattr(sys, "real_prefix", None)
    if prefix:
        path = os.path.join(prefix, "bin", "python")
        if is_executable(path):
            return path


def target_python(desired=None, package_name=None, fatal=True):
    """
    :param str|None desired: Desired python (overrides anything else configured)
    :param str|None package_name: Target pypi package
    :param bool fatal: If True, abort execution if python invalid
    :return PythonInstallation: Python installation to use
    """
    if not desired:
        desired = DESIRED_PYTHON or SETTINGS.resolved_value("python", package_name=package_name) or INVOKER
    python = PythonInstallation(desired)
    if not python.is_valid:
        return abort(python.problem, fatal=fatal, quiet=not fatal, return_value=python)

    return python


class PythonInstallation:

    text = None  # type: str # Given description or path
    executable = None  # type: str # Full path to python executable
    major = None  # type: str # Major version
    minor = None  # type: str # Minor version

    def __init__(self, text):
        """
        :param str text: Python, of the form: python, or python3.6, or py36, or full path to exe
        """
        self.text = text
        if text == INVOKER:
            text = parent_python() or sys.executable
        if os.path.isabs(text):
            self._set_executable(text)
            return
        m = RE_PYTHON_LOOSE.match(text)
        if m:
            self.major = m.group(3)
            self.minor = m.group(4)
            self.resolve_executable()

    def _set_executable(self, path):
        path = resolved_path(path)
        if is_executable(path):
            self.executable = path
            if not self.major or not self.minor:
                output = run_program(self.executable, "--version", dryrun=False, fatal=False, include_error=True, quiet=True)
                if output:
                    m = RE_PYTHON_LOOSE.match(output)
                    if m:
                        self.major = m.group(3)
                        self.minor = m.group(4)

    def resolve_executable(self):
        """Resolve executable from given major/minor"""
        self._set_executable(self._resolve_from_configured(SETTINGS.get_value("python_installs")))
        if not self.executable:
            self._set_executable(which(self.program_name, ignore_own_venv=True))

    def _resolve_from_configured(self, folder):
        """
        Resolve python executable from a configured folder
        This aims to support pyenv like installations, as well as /usr/bin-like ones
        """
        folder = resolved_path(folder)
        if not folder or not self.major or not os.path.isdir(folder):
            return None

        timestamp = None
        result = None
        interesting = [self.program_name, "%s.%s" % (self.major, self.minor or "")]
        for fname in os.listdir(folder):
            m = RE_PYTHON_STRICT.match(fname)
            if not m:
                continue
            m = m.group(2) or m.group(3)
            if not m or not any(m.startswith(x) for x in interesting):
                continue
            ts = os.path.getmtime(os.path.join(folder, fname))
            if timestamp is None or timestamp < ts:
                timestamp = ts
                result = fname

        return result and self._first_executable(
            os.path.join(folder, result, "bin", "python"),
            os.path.join(folder, result),
        )

    def _first_executable(self, *paths):
        for path in paths:
            if is_executable(path):
                return path
        return None

    def __repr__(self):
        if self.is_valid:
            return "%s [%s.%s]" % (self.executable, self.major, self.minor)
        return self.program_name

    def shebang(self, universal=False):
        """
        :param bool universal: True if produced package is universal
        :return str: Shebang to use
        """
        if DESIRED_PYTHON and os.path.isabs(DESIRED_PYTHON):
            return DESIRED_PYTHON
        if universal:
            return "/usr/bin/env python"
        if os.path.isabs(self.text):
            return self.executable
        return "/usr/bin/env %s" % self.program_name

    @property
    def problem(self):
        if not self.major or not self.minor:
            if self.major:
                return "'%s' is not a valid python installation" % (self.executable or self.program_name)
            return "No python installation '%s' found" % self.text
        if not self.executable:
            return "%s is not installed" % self.program_name
        return None

    @property
    def is_valid(self):
        return self.problem is None

    @property
    def short_name(self):
        return "py%s%s" % (self.major, self.minor)

    @property
    def program_name(self):
        if self.major and self.minor:
            return "python%s.%s" % (self.major, self.minor)
        elif self.major:
            return "python%s" % self.major
        elif self.text and self.text.startswith("python"):
            return self.text
        return "python '%s'" % self.text
