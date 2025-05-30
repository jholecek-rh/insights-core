import codecs
import itertools
import logging
import os
import re
import shlex
import signal
import six
import traceback

from collections import defaultdict
from glob import glob
from subprocess import call

from insights.cleaner import DEFAULT_OBFUSCATIONS
from insights.cleaner.filters import AllowFilter
from insights.core import blacklist, dr, filters
from insights.core.context import ExecutionContext, FSRoots, HostContext
from insights.core.exceptions import (
    BlacklistedSpec,
    ContentException,
    NoFilterException,
    SkipComponent,
)
from insights.core.plugins import component, datasource, is_datasource
from insights.core.serde import deserializer, serializer
from insights.util import fs, streams, which
from insights.util.mangle import mangle_command

log = logging.getLogger(__name__)

MAX_CONTENT_SIZE = 104857600 * 2  # 200 MB
SAFE_ENV = {
    "PATH": os.path.pathsep.join(
        [
            "/bin",
            "/usr/bin",
            "/sbin",
            "/usr/sbin",
            "/usr/share/Modules/bin",
        ]
    ),
    "LC_ALL": "C",
}
"""
A minimal set of environment variables for use in subprocess calls
"""
if "LANG" in os.environ:
    SAFE_ENV["LANG"] = os.environ["LANG"]

safe_open, encoding = (open, "utf-8") if six.PY3 else (codecs.open, None)


class ContentProvider(object):
    def __init__(self):
        self.cmd = None
        self.args = None
        self.rc = None
        self.root = None
        self.relative_path = None
        self.save_as = None
        self.loaded = False
        self._content = None
        self._exception = None
        self._filterable = False
        self._filters = dict()

    def load(self):
        raise NotImplementedError()

    def stream(self):
        """
        Returns a generator of lines instead of a list of lines.
        """
        st = self._stream()
        for l in next(st):
            yield l.rstrip("\n")

    def _stream(self):
        raise NotImplementedError()

    def _clean_content(self):
        """
        Clean (Redact, Filter, and Obfuscate) the Spec Content ONLY when
        collecting data.
        """
        content = self.content  # load first for debugging info order
        if content and isinstance(self.ctx, HostContext) and self.ds and self.cleaner:
            cleans = []
            # Redacting?
            no_red = getattr(self.ds, 'no_redact', False)
            cleans.append("Redact") if not no_red else None
            # Obfuscating?
            no_obf = getattr(self.ds, 'no_obfuscate', [])
            cleans.append("Obfuscate") if set(no_obf) != DEFAULT_OBFUSCATIONS else None
            # Filtering?
            allowlist = None
            if self._filterable:
                cleans.append("Filter")
                allowlist = self._filters
            # Cleaning - Entry
            if cleans:
                log.debug("Cleaning (%s) %s", "/".join(cleans), self.relative_path)
                content = self.cleaner.clean_content(
                    content,
                    no_obfuscate=no_obf,
                    allowlist=allowlist,
                    no_redact=no_red,
                    width=self.relative_path.endswith("netstat_-neopa"),
                )
                if len(content) == 0:
                    log.debug("Skipping %s due to empty after cleaning", self.path)
                    raise ContentException("Empty after cleaning: %s" % self.path)
            else:
                log.debug("Skipping cleaning %s", self.relative_path)
        return content

    @property
    def path(self):
        return os.path.join(self.root, self.relative_path)

    @property
    def content(self):
        if self._exception:
            raise self._exception

        if self._content is None and not self.loaded:
            try:
                self._content = self.load()
            except Exception as ex:
                self._exception = ex
                raise

        if len(self._content) == 0:
            log.debug("File is empty (after filtering): %s", self.path)
            if isinstance(self.ctx, HostContext):
                # Do not collect empty spec
                raise ContentException("Empty (after filtering): %s" % self.path)

        return self._content

    def write(self, dst):
        fs.ensure_path(os.path.dirname(dst))
        # Clean Spec Content when writing it down to disk before uploading
        content = "\n".join(self._clean_content())
        content = content.encode("utf-8") if six.PY3 else content
        with open(dst, "wb") as f:
            f.write(content)

        self.loaded = False

    def __repr__(self):
        msg = "<%s(path=%r, cmd=%r)>"
        return msg % (self.__class__.__name__, self.path or "", self.cmd or "")

    def __unicode__(self):
        return self.__repr__()

    def __str__(self):
        return self.__unicode__()


class DatasourceProvider(ContentProvider):
    def __init__(
        self,
        content,
        relative_path,
        root='/',
        save_as=None,
        ds=None,
        ctx=None,
        cleaner=None,
        no_obfuscate=None,
        no_redact=False,
    ):
        super(DatasourceProvider, self).__init__()
        self.relative_path = relative_path.lstrip("/")
        self.save_as = save_as
        self._content = content if isinstance(content, list) else content.splitlines()
        self.root = root
        self.ds = ds or self
        self.ctx = ctx
        self.cleaner = cleaner
        self.no_obfuscate = no_obfuscate or []
        self.no_redact = no_redact

    def _stream(self):
        """
        Returns a generator of lines instead of a list of lines.
        """
        yield self._content

    def load(self):
        self.loaded = True
        return self.content


class FileProvider(ContentProvider):
    def __init__(self, relative_path, root="/", save_as=None, ds=None, ctx=None, cleaner=None):
        super(FileProvider, self).__init__()
        self.ds = ds
        self.ctx = ctx
        self.root = root
        self.cleaner = cleaner
        self.relative_path = relative_path.lstrip("/")
        self.save_as = save_as
        self.file_name = os.path.basename(self.path)
        self._filterable = (
            any(s.filterable for s in dr.get_registry_points(self.ds))
            if self.ds and filters.ENABLED
            else False
        )
        self._filters = filters.get_filters(self.ds, True) if self.ds else dict()

        self.validate()

    def validate(self):
        # 1. No Such File
        if not os.path.exists(self.path):
            raise ContentException("%s does not exist." % self.path)
        # 2. Check only when collecting
        if isinstance(self.ctx, HostContext):
            # 2.1 No Filters for 'filterable=True' Specs
            if self._filterable and not self._filters:
                raise NoFilterException("Skipping %s due to no filters." % dr.get_name(self.ds))
            # 2.2 Customer Prohibits Collection
            if not blacklist.allow_file("/" + self.relative_path):
                log.warning("WARNING: Skipping file %s", "/" + self.relative_path)
                raise BlacklistedSpec()

        resolved = os.path.realpath(self.path)
        if not resolved.startswith(os.path.realpath(self.root)):
            msg = "Relative path points outside the root: %s -> %s."
            raise Exception(msg % (self.path, resolved))

        if not os.access(self.path, os.R_OK):
            raise ContentException("Cannot access %s" % self.path)

    def __repr__(self):
        return '%s("%r")' % (self.__class__.__name__, self.path)


class RawFileProvider(FileProvider):
    """
    Class used in datasources that returns the contents of a file a single
    string.

    .. note::

        The content of RawFileProvider is not filtered/obfuscated/redacted.
    """

    def load(self):
        self.loaded = True
        with open(self.path, 'rb') as f:
            return f.read()

    def write(self, dst):
        fs.ensure_path(os.path.dirname(dst))
        call([which("cp", env=SAFE_ENV), self.path, dst], env=SAFE_ENV)


class TextFileProvider(FileProvider):
    """
    Class used in datasources that returns the contents of a file a list of
    lines. Each line is filtered if filters are defined for the datasource.
    """

    def create_args(self):
        """
        The "grep" is faster and can be used shrink the size of file.
        """
        args = []
        if isinstance(self.ctx, HostContext) and self._filters:
            # Pre-filtering ONLY when collecting data
            log.debug("Pre-filtering %s", self.relative_path)
            args.append(
                ["grep", "-F", "--", "\n".join(self._filters.keys()), self.path]
            )

        return args

    def load(self):
        self.loaded = True
        args = self.create_args()
        if args:
            rc, out = self.ctx.shell_out(args, keep_rc=True, env=SAFE_ENV)
            self.rc = rc
            return out

        fsize = os.stat(self.path).st_size
        with safe_open(self.path, "r", encoding=encoding, errors="surrogateescape") as f:
            if fsize > MAX_CONTENT_SIZE:
                # read the last ``MAX_CONTENT_SIZE`` MB only
                f.seek(fsize - MAX_CONTENT_SIZE)
                log.debug("Extra-huge file is truncated %s", self.relative_path)
                content = [l.rstrip("\n") for l in f][1:]  # discard the first line which is broken
            else:
                content = [l.rstrip("\n") for l in f]
            if not isinstance(self.ctx, HostContext) and self._filters:
                # Post-filtering ONLY when processing data
                content = AllowFilter.filter_content(content, self._filters)
            return content

    def _stream(self):
        """
        Returns a generator of lines instead of a list of lines.
        """
        if self._exception:
            raise self._exception
        try:
            if self._content:
                yield self._content
            else:
                args = self.create_args()
                if args:
                    with streams.connect(*args, env=SAFE_ENV) as s:
                        yield s
                else:
                    with safe_open(
                        self.path, "r", encoding=encoding, errors="surrogateescape"
                    ) as f:
                        yield f
        except StopIteration:
            raise
        except Exception as ex:
            self._exception = ex
            raise ContentException(str(ex))


class SerializedOutputProvider(TextFileProvider):
    pass


class SerializedRawOutputProvider(RawFileProvider):
    pass


class CommandOutputProvider(ContentProvider):
    """
    Class used in datasources to return output from commands.
    """

    def __init__(
        self,
        cmd,
        ctx,
        root="insights_commands",
        save_as=None,
        args=None,
        split=True,
        keep_rc=False,
        ds=None,
        timeout=None,
        inherit_env=None,
        override_env=None,
        signum=None,
        cleaner=None,
    ):
        super(CommandOutputProvider, self).__init__()
        self.cmd = cmd if six.PY3 else str(cmd)
        self.root = root
        self.save_as = save_as
        self.ctx = ctx
        self.args = args  # already interpolated into cmd - stored here for context.
        self.split = split
        self.keep_rc = keep_rc
        self.ds = ds
        self.timeout = timeout
        self.inherit_env = inherit_env if inherit_env is not None else []
        self.override_env = override_env if override_env is not None else dict()
        self.signum = signum or signal.SIGKILL
        self.rc = None
        self.cleaner = cleaner

        self._misc_settings()
        self._content = None
        self._env = self.create_env()
        self._filterable = (
            any(s.filterable for s in dr.get_registry_points(self.ds))
            if self.ds and filters.ENABLED
            else False
        )
        self._filters = filters.get_filters(self.ds, True) if self.ds else dict()

        self.validate()

    def _misc_settings(self):
        """Re-implement it according to the actual case of the sub-class"""
        self.relative_path = mangle_command(self.cmd)

    def validate(self):
        # 1. No Such Command
        cmd = shlex.split(self.cmd)[0]
        if not which(cmd, env=self._env):
            raise ContentException("Command not found: %s" % cmd)
        # 2. Check only when collecting
        if isinstance(self.ctx, HostContext):
            # 2.1 No Filters for 'filterable=True' Specs
            if self._filterable and not self._filters:
                raise NoFilterException("Skipping %s due to no filters." % dr.get_name(self.ds))
            # 2.2 Customer Prohibits Collection
            if not blacklist.allow_command(self.cmd):
                log.warning("WARNING: Skipping command %s", self.cmd)
                raise BlacklistedSpec()

    def create_args(self):
        command = [shlex.split(self.cmd)]

        if self.split and self._filters:
            log.debug("Pre-filtering  %s", self.relative_path)
            command.append(["grep", "-F", "--", "\n".join(self._filters.keys())])

        return command

    def create_env(self):
        env = dict(SAFE_ENV)

        for e in self.inherit_env:
            if e in os.environ:
                env[e] = os.environ[e]

        for k, v in self.override_env.items():
            env[k] = v

        return env

    def load(self):
        command = self.create_args()

        raw = self.ctx.shell_out(
            command,
            split=self.split,
            keep_rc=self.keep_rc,
            timeout=self.timeout,
            env=self._env,
            signum=self.signum,
        )
        if self.keep_rc:
            self.rc, output = raw
        else:
            output = raw
        return output

    def _stream(self):
        """
        Returns a generator of lines instead of a list of lines.
        """
        if self._exception:
            raise self._exception
        try:
            if self._content:
                yield self._content
            else:
                command = self.create_args()
                with self.ctx.connect(*command, env=self._env, timeout=self.timeout) as s:
                    yield s
        except StopIteration:
            raise
        except Exception as ex:
            self._exception = ex
            raise ContentException(str(ex))

    def __repr__(self):
        return 'CommandOutputProvider("%r")' % self.cmd


class ContainerProvider(CommandOutputProvider):
    def __init__(
        self,
        cmd_path,
        ctx,
        image=None,
        args=None,
        split=True,
        keep_rc=False,
        ds=None,
        timeout=None,
        inherit_env=None,
        override_env=None,
        signum=None,
        cleaner=None,
    ):
        # cmd  = "<podman|docker> exec container_id command"
        # path = "<podman|docker> exec container_id cat path"
        self.image = image
        super(ContainerProvider, self).__init__(
            cmd_path,
            ctx,
            "insights_containers",
            None,
            args,
            split,
            keep_rc,
            ds,
            timeout,
            inherit_env,
            override_env,
            signum,
            cleaner,
        )


class ContainerFileProvider(ContainerProvider):
    def _misc_settings(self):
        engine, _, container_id, _, path = self.cmd.split(None, 4)
        self.engine = os.path.basename(engine)
        self.container_id = container_id
        self.relative_path = os.path.join(container_id, path.lstrip('/'))

    def __repr__(self):
        return 'ContainerFileProvider("%r")' % self.cmd


class ContainerCommandProvider(ContainerProvider):
    def _misc_settings(self):
        engine, _, container_id, cmd = self.cmd.split(None, 3)
        self.engine = os.path.basename(engine)
        self.container_id = container_id
        self.relative_path = os.path.join(container_id, "insights_commands", mangle_command(cmd))

    def __repr__(self):
        return 'ContainerCommandProvider("%r")' % self.cmd


class RegistryPoint(object):
    # Marker class for declaring that an element of a `SpecSet` subclass
    # is a registry point against which further subclasses can register
    # datasource implementations by simply declaring them with the same name.
    #
    # intentionally not a docstring so this doesn't show up in pydoc.
    def __init__(
        self,
        metadata=None,
        multi_output=False,
        raw=False,
        filterable=False,
        no_obfuscate=None,
        no_redact=False,
        prio=0,
    ):
        self.metadata = metadata
        self.multi_output = multi_output
        self.no_obfuscate = [] if no_obfuscate is None else no_obfuscate
        self.no_redact = no_redact
        self.prio = prio
        self.raw = raw
        self.filterable = filterable
        self.__name__ = self.__class__.__name__
        datasource(
            [],
            metadata=metadata,
            multi_output=multi_output,
            raw=raw,
            filterable=filterable,
            no_obfuscate=self.no_obfuscate,
            no_redact=no_redact,
            prio=prio,
        )(self)

    def __call__(self, broker):
        for c in reversed(dr.get_delegate(self).deps):
            if c in broker:
                return broker[c]
        raise SkipComponent()

    def __repr__(self):
        return dr.get_name(self)


class SpecDescriptor(object):
    # Descriptor Protocol handler that returns the literal function from a
    # class during dot (.) access.
    #
    # intentionally not a docstring so this doesn't show up in pydoc.
    def __init__(self, func):
        self.func = func

    def __get__(self, obj, obj_type):
        return self.func

    def __set__(self, obj, val):
        raise AttributeError()


def _get_ctx_dependencies(component):
    ctxs = set()
    for c in dr.walk_tree(component):
        try:
            if issubclass(c, ExecutionContext):
                ctxs.add(c)
        except Exception:
            pass
    return ctxs


def _register_context_handler(parents, component):
    name = component.__name__
    parents = list(itertools.takewhile(lambda x: name in x.registry, parents))
    if not parents:
        return

    # If the new component handles a context, we need to tell the
    # previously registered components that would have handled it to ignore it.

    # The components that handle a context are registered on the highest class
    # of the MRO list. This is so overrides work correctly even if a
    # component isn't a direct sibling of the component it's overriding.

    # instead of trying to unhook all of the dependencies, we just tell the
    # previous handler of a context to ignore it.
    ctx_handlers = parents[-1].context_handlers
    for c in _get_ctx_dependencies(component):
        for old in ctx_handlers[name][c]:
            dr.add_ignore(old, c)
        ctx_handlers[name][c].append(component)


def _resolve_registry_points(cls, base, dct):
    module = cls.__module__
    parents = [x for x in cls.__mro__ if x not in (cls, SpecSet, object)]

    for k, v in dct.items():
        if isinstance(v, RegistryPoint):
            # add v under its name to this class's registry.
            v.__name__ = k
            cls.registry[k] = v

        if is_datasource(v):
            v.__qualname__ = ".".join([cls.__name__, k])
            v.__name__ = k
            v.__module__ = module
            setattr(cls, k, SpecDescriptor(v))
            if k in base.registry:
                # if the datasource has the same name as a RegistryPoint in the
                # base class, the datasource to the RegistryPoint.
                point = base.registry[k]

                # TODO: log when RegistryPoint and implementation properties
                # TODO: aren't the same.
                delegate = dr.get_delegate(v)
                v.filterable = delegate.filterable = point.filterable
                v.raw = delegate.raw = point.raw
                v.multi_output = delegate.multi_output = point.multi_output
                v.no_obfuscate = delegate.no_obfuscate = point.no_obfuscate
                v.no_redact = delegate.no_redact = point.no_redact
                v.prio = delegate.prio = point.prio

                # the RegistryPoint gets the implementation datasource as a
                # dependency
                dr.add_dependency(point, v)

                # Datasources override previously defined datasources of the
                # same name for contexts they all depend on. Here we tell
                # datasources of the same name not to execute under contexts
                # the new datasource will handle.
                _register_context_handler(parents, v)


class SpecSetMeta(type):
    """
    The metaclass that converts RegistryPoint markers to registry point
    datasources and hooks implementations for them into the registry.
    """

    def __new__(cls, name, bases, dct):
        dct["context_handlers"] = defaultdict(lambda: defaultdict(list))
        dct["registry"] = {}
        return super(SpecSetMeta, cls).__new__(cls, name, bases, dct)

    def __init__(cls, name, bases, dct):
        if name == "SpecSet":
            return
        if len(bases) > 1:
            raise Exception("SpecSet subclasses must inherit from only one class.")
        _resolve_registry_points(cls, bases[0], dct)


class SpecSet(six.with_metaclass(SpecSetMeta)):
    """
    The base class for all spec declarations. Extend this class and define your
    datasources directly or with a `SpecFactory`.
    """

    pass


def _get_context(context, broker):
    if isinstance(context, list):
        return dr.first_of(context, broker)
    return broker.get(context)


class simple_file(object):
    """
    Creates a datasource that reads the file at path when evaluated.

    Args:
        path (str): path to the file to collect.
        save_as (str or None): path to save the collected file as.
            - It should be a relative path and any starting '/' will be removed.
            - If it's a path which ending with '/', the collected file will be
            stored to the "save_as" directory,
            - If it's a path which not ending with '/', the collected file will
            be renamed to the file with "save_as" as the full path.
        context (ExecutionContext): the context under which the datasource
            should run.
        kind (FileProvider): One of TextFileProvider or RawFileProvider.

    Returns:
        function: A datasource that reads all files matching the glob patterns.
    """

    def __init__(
        self, path, save_as=None, context=None, deps=None, kind=TextFileProvider, **kwargs
    ):
        deps = deps if deps is not None else []
        self.path = path
        self.save_as = save_as.lstrip("/") if save_as else None
        self.context = context or FSRoots
        self.kind = kind
        self.raw = kind is RawFileProvider
        self.__name__ = self.__class__.__name__
        datasource(self.context, *deps, raw=self.raw, **kwargs)(self)

    def __call__(self, broker):
        ctx = _get_context(self.context, broker)
        cleaner = broker.get('cleaner')
        return self.kind(
            ctx.locate_path(self.path),
            root=ctx.root,
            save_as=self.save_as,
            ds=self,
            ctx=ctx,
            cleaner=cleaner,
        )


class glob_file(object):
    """
    Creates a datasource that reads all files matching the glob pattern(s).

    Args:
        patterns (str or [str]): glob pattern(s) of paths to collect.
        save_as (str or None): directory path to save the collected files as.
            - It should be a relative path and any starting '/' will be removed
            and an ending '/' will be added.
        ignore (regex): a regular expression that is used to filter the paths
            matched by pattern(s).
        context (ExecutionContext): the context under which the datasource
            should run.
        kind (FileProvider): One of TextFileProvider or RawFileProvider.
        max_files (int): Maximum number of glob files to process.

    Returns:
        function: A datasource that reads all files matching the glob patterns.
    """

    def __init__(
        self,
        patterns,
        save_as=None,
        ignore=None,
        context=None,
        deps=None,
        kind=TextFileProvider,
        max_files=1000,
        **kwargs
    ):
        deps = deps if deps is not None else []
        if not isinstance(patterns, (list, set)):
            patterns = [patterns]
        self.patterns = patterns
        self.ignore = ignore
        self.save_as = os.path.join(save_as.lstrip("/"), '') if save_as else None
        self.ignore_func = re.compile(ignore).search if ignore else lambda x: False
        self.context = context or FSRoots
        self.kind = kind
        self.raw = kind is RawFileProvider
        self.max_files = max_files
        self.__name__ = self.__class__.__name__
        datasource(self.context, *deps, multi_output=True, raw=self.raw, **kwargs)(self)

    def __call__(self, broker):
        cleaner = broker.get('cleaner')
        ctx = _get_context(self.context, broker)
        root = ctx.root
        results = []
        for pattern in self.patterns:
            pattern = ctx.locate_path(pattern)
            for path in sorted(glob(os.path.join(root, pattern.lstrip('/')))):
                if self.ignore_func(path) or os.path.isdir(path):
                    continue
                try:
                    results.append(
                        self.kind(
                            path[len(root) :],
                            root=root,
                            save_as=self.save_as,
                            ds=self,
                            ctx=ctx,
                            cleaner=cleaner,
                        )
                    )
                except NoFilterException as nfe:
                    raise nfe
                except Exception:
                    log.debug(traceback.format_exc())
        if results:
            if len(results) > self.max_files:
                raise ContentException(
                    "Number of files returned [{0}] is over the {1} file limit, please refine "
                    "the specs file pattern to narrow down results".format(
                        len(results), self.max_files
                    )
                )
            return results
        raise ContentException("[%s] didn't match." % ', '.join(self.patterns))


class head(object):
    """
    Return the first element of any datasource that produces a list.
    """

    def __init__(self, dep, **kwargs):
        self.dep = dep
        self.__name__ = self.__class__.__name__
        datasource(dep, **kwargs)(self)

    def __call__(self, lst):
        c = lst[self.dep]
        if lst:
            return c[0]
        raise SkipComponent()


class first_file(object):
    """
    Creates a datasource that returns the first existing and readable file in
    files.

    Args:
        paths (str): list of paths to find and collect.
        save_as (str or None): path to save the collected file as.
            - It should be a relative path and any starting '/' will be removed.
            - If it's a path which ending with '/', the collected file will be
            stored to the "save_as" directory,
            - If it's a path which not ending with '/', the collected file will
            be renamed to the file with "save_as" as the full path.
        context (ExecutionContext): the context under which the datasource
            should run.
        kind (FileProvider): One of TextFileProvider or RawFileProvider.

    Returns:
        function: A datasource that returns the first file in files that exists
            and is readable
    """

    def __init__(
        self, paths, save_as=None, context=None, deps=None, kind=TextFileProvider, **kwargs
    ):
        deps = deps if deps is not None else []
        self.paths = paths
        self.save_as = save_as.lstrip("/") if save_as else None
        self.context = context or FSRoots
        self.kind = kind
        self.raw = kind is RawFileProvider
        self.__name__ = self.__class__.__name__
        datasource(self.context, *deps, raw=self.raw, **kwargs)(self)

    def __call__(self, broker):
        cleaner = broker.get('cleaner')
        ctx = _get_context(self.context, broker)
        root = ctx.root
        for p in self.paths:
            try:
                return self.kind(
                    ctx.locate_path(p),
                    root=root,
                    save_as=self.save_as,
                    ds=self,
                    ctx=ctx,
                    cleaner=cleaner,
                )
            except NoFilterException as nfe:
                raise nfe
            except Exception:
                pass
        raise ContentException("None of [%s] found." % ', '.join(self.paths))


class listdir(object):
    """
    Execute a simple directory listing of all the files and directories in
    path.

    Args:
        path (str): directory to list.
        context (ExecutionContext): the context under which the datasource
            should run.
        ignore (str): regular expression defining names to ignore.

    Returns:
        function: A datasource that returns a sorted list of file and directory
            names in the directory specified by path. The list will be empty when
            the directory is empty or all names get ignored.
    """

    def __init__(self, path, context=None, ignore=None, deps=None):
        deps = deps if deps is not None else []
        self.path = path
        self.context = context or FSRoots
        self.ignore = ignore
        self.ignore_func = re.compile(ignore).search if ignore else lambda x: False
        self.__name__ = self.__class__.__name__
        datasource(self.context, *deps)(self)

    def __call__(self, broker):
        ctx = _get_context(self.context, broker)
        p = os.path.join(ctx.root, self.path.lstrip('/'))
        p = ctx.locate_path(p)
        try:
            result = os.listdir(p)
        except OSError as e:
            raise ContentException(str(e))
        return sorted([r for r in result if not self.ignore_func(r)])


class listglob(listdir):
    """
    List paths matching a glob pattern.

    Args:
        pattern (str): glob pattern to list.
        context (ExecutionContext): the context under which the datasource
            should run.
        ignore (str): regular expression defining paths to ignore.

    Returns:
        function: A datasource that returns the list of paths that match
            the given glob pattern. The list will be empty when nothing matches.
    """

    def __call__(self, broker):
        ctx = _get_context(self.context, broker)
        p = os.path.join(ctx.root, self.path.lstrip('/'))
        p = ctx.locate_path(p)
        result = glob(p)
        # generator expression; we don't need the full list at this step
        result = (os.path.relpath(r, start=ctx.root) for r in result)
        result = sorted([r for r in result if not self.ignore_func(r)])
        return result


class simple_command(object):
    """
    Execute a simple command that has no dynamic arguments

    Args:
        cmd (str): the command(s) to execute. Breaking apart a command
            string that might contain multiple commands separated by a pipe,
            getting them ready for subproc operations.
            IE. A command with filters applied
        save_as (str or None): path to save the collected file as.
            - It should be a relative path in which any starting and ending
            '/' will be removed, the collected file will be renamed to
            `save_as` under the 'insights_commands' directory.
        context (ExecutionContext): the context under which the datasource
            should run.
        split (bool): whether the output of the command should be split into a
            list of lines
        keep_rc (bool): whether to return the error code returned by the
            process executing the command. If False, any return code other than
            zero with raise a CalledProcessError. If True, the return code and
            output are always returned.
        timeout (int): Number of seconds to wait for the command to complete.
            If the timeout is reached before the command returns, a
            CalledProcessError is raised. If None, timeout is infinite.
        inherit_env (list): The list of environment variables to inherit from the
            calling process when the command is invoked.
        override_env (dict): A dict of environment variables to override from the
            calling process when the command is invoked.

    Returns:
        function: A datasource that returns the output of a command that takes
            no arguments
    """

    def __init__(
        self,
        cmd,
        save_as=None,
        context=HostContext,
        deps=None,
        split=True,
        keep_rc=False,
        timeout=None,
        inherit_env=None,
        override_env=None,
        signum=None,
        **kwargs
    ):
        deps = deps if deps is not None else []
        self.cmd = cmd
        self.context = context
        self.save_as = save_as.strip("/") if save_as else None  # strip as a relative file path
        self.split = split
        self.raw = not split
        self.keep_rc = keep_rc
        self.timeout = timeout
        self.inherit_env = inherit_env if inherit_env is not None else []
        self.override_env = override_env if override_env is not None else dict()
        self.signum = signum
        self.__name__ = self.__class__.__name__
        datasource(self.context, *deps, raw=self.raw, **kwargs)(self)

    def __call__(self, broker):
        cleaner = broker.get('cleaner')
        ctx = broker[self.context]
        return CommandOutputProvider(
            self.cmd,
            ctx,
            save_as=self.save_as,
            split=self.split,
            keep_rc=self.keep_rc,
            ds=self,
            timeout=self.timeout,
            inherit_env=self.inherit_env,
            override_env=self.override_env,
            signum=self.signum,
            cleaner=cleaner,
        )


class command_with_args(object):
    """
    Execute a command that has dynamic arguments

    Args:
        cmd (str): the command to execute. Breaking apart a command
            string that might require arguments.
        provider (str or tuple): argument string or a tuple of argument strings.
        save_as (str or None): path to save the collected file as.
            - It should be a relative path in which any starting and ending
            '/' will be removed, the collected file will be renamed to
            `save_as` under the 'insights_commands' directory.
        context (ExecutionContext): the context under which the datasource
            should run.
        split (bool): whether the output of the command should be split into a
            list of lines
        keep_rc (bool): whether to return the error code returned by the
            process executing the command. If False, any return code other than
            zero with raise a CalledProcessError. If True, the return code and
            output are always returned.
        timeout (int): Number of seconds to wait for the command to complete.
            If the timeout is reached before the command returns, a
            CalledProcessError is raised. If None, timeout is infinite.
        inherit_env (list): The list of environment variables to inherit from the
            calling process when the command is invoked.
        override_env (dict): A dict of environment variables to override from the
            calling process when the command is invoked.

    Returns:
        function: A datasource that returns the output of a command that takes
            specified arguments passed by the provider.
    """

    def __init__(
        self,
        cmd,
        provider,
        save_as=None,
        context=HostContext,
        deps=None,
        split=True,
        keep_rc=False,
        timeout=None,
        inherit_env=None,
        override_env=None,
        signum=None,
        **kwargs
    ):
        deps = deps if deps is not None else []
        self.cmd = cmd if six.PY3 else str(cmd)
        self.provider = provider
        self.save_as = save_as.strip("/") if save_as else None  # strip as a relative file path
        self.context = context
        self.split = split
        self.raw = not split
        self.keep_rc = keep_rc
        self.timeout = timeout
        self.inherit_env = inherit_env if inherit_env is not None else []
        self.override_env = override_env if override_env is not None else dict()
        self.signum = signum
        self.__name__ = self.__class__.__name__
        datasource(self.provider, self.context, *deps, raw=self.raw, **kwargs)(self)

    def __call__(self, broker):
        cleaner = broker.get('cleaner')
        source = broker[self.provider]
        ctx = broker[self.context]
        if isinstance(source, ContentProvider):
            source = source.content
        if not isinstance(source, (six.text_type, str, tuple)):
            raise ContentException(
                "The provider can only be a single string or a tuple of strings, but got '%s'."
                % source
            )
        try:
            the_cmd = self.cmd % source
            return CommandOutputProvider(
                the_cmd,
                ctx,
                save_as=self.save_as,
                split=self.split,
                keep_rc=self.keep_rc,
                ds=self,
                timeout=self.timeout,
                inherit_env=self.inherit_env,
                override_env=self.override_env,
                signum=self.signum,
                cleaner=cleaner,
            )
        except NoFilterException as nfe:
            raise nfe
        except ContentException as ce:
            log.debug(ce)
        except Exception:
            log.debug(traceback.format_exc())
        raise ContentException("No results found for [%s]" % self.cmd)


class foreach_execute(object):
    """
    Execute a command for each element in provider. Provider is the output of
    a different datasource that returns a list of single elements or a list of
    tuples. The command should have %s substitution parameters equal to the
    number of elements in each tuple of the provider.

    Args:
        provider (list): a list of elements or tuples.
        cmd (str): a command with substitution parameters. Breaking
            apart a command string that might contain multiple commands
            separated by a pipe, getting them ready for subproc operations.
            IE. A command with filters applied
        context (ExecutionContext): the context under which the datasource
            should run.
        split (bool): whether the output of the command should be split into a
            list of lines
        keep_rc (bool): whether to return the error code returned by the
            process executing the command. If False, any return code other than
            zero with raise a CalledProcessError. If True, the return code and
            output are always returned.
        timeout (int): Number of seconds to wait for the command to complete.
            If the timeout is reached before the command returns, a
            CalledProcessError is raised. If None, timeout is infinite.
        inherit_env (list): The list of environment variables to inherit from the
            calling process when the command is invoked.
        override_env (dict): A dict of environment variables to override from the
            calling process when the command is invoked.

    Returns:
        function: A datasource that returns a list of outputs for each command
            created by substituting each element of provider into the cmd template.
    """

    def __init__(
        self,
        provider,
        cmd,
        context=HostContext,
        deps=None,
        split=True,
        keep_rc=False,
        timeout=None,
        inherit_env=None,
        override_env=None,
        signum=None,
        **kwargs
    ):
        deps = deps if deps is not None else []
        self.provider = provider
        self.cmd = cmd
        self.context = context
        self.split = split
        self.raw = not split
        self.keep_rc = keep_rc
        self.timeout = timeout
        self.inherit_env = inherit_env if inherit_env is not None else []
        self.override_env = override_env if override_env is not None else dict()
        self.signum = signum
        self.__name__ = self.__class__.__name__
        datasource(self.provider, self.context, *deps, multi_output=True, raw=self.raw, **kwargs)(
            self
        )

    def __call__(self, broker):
        result = []
        source = broker[self.provider]
        cleaner = broker.get('cleaner')
        ctx = broker[self.context]
        if isinstance(source, ContentProvider):
            source = source.content
        if not isinstance(source, (list, set)):
            source = [source]
        for e in source:
            try:
                the_cmd = self.cmd % e
                cop = CommandOutputProvider(
                    the_cmd,
                    ctx,
                    args=e,
                    split=self.split,
                    keep_rc=self.keep_rc,
                    ds=self,
                    timeout=self.timeout,
                    inherit_env=self.inherit_env,
                    override_env=self.override_env,
                    signum=self.signum,
                    cleaner=cleaner,
                )
                result.append(cop)
            except NoFilterException as nfe:
                raise nfe
            except ContentException as ce:
                log.debug(ce)
            except Exception:
                log.debug(traceback.format_exc())
        if result:
            return result
        raise ContentException("No results found for [%s]" % self.cmd)


class foreach_collect(object):
    """
    Subtitutes each element in provider into path and collects the files at the
    resulting paths.

    Args:
        provider (list): a list of elements or tuples.
        save_as (str or None): directory path to save the collected files as.
            - It should be a relative path and any starting '/' will be removed
            and an ending '/' will be added.
        path (str): a path template with substitution parameters.
        context (ExecutionContext): the context under which the datasource
            should run.
        kind (FileProvider): one of TextFileProvider or RawFileProvider

    Returns:
        function: A datasource that returns a list of file contents created by
            substituting each element of provider into the path template.
    """

    def __init__(
        self,
        provider,
        path,
        save_as=None,
        ignore=None,
        context=HostContext,
        deps=None,
        kind=TextFileProvider,
        **kwargs
    ):
        deps = deps if deps is not None else []
        self.provider = provider
        self.path = path
        self.save_as = os.path.join(save_as.lstrip("/"), '') if save_as else None
        self.ignore = ignore
        self.ignore_func = re.compile(ignore).search if ignore else lambda x: False
        self.context = context
        self.kind = kind
        self.raw = kind is RawFileProvider
        self.__name__ = self.__class__.__name__
        datasource(self.provider, self.context, *deps, multi_output=True, raw=self.raw, **kwargs)(
            self
        )

    def __call__(self, broker):
        result = []
        source = broker[self.provider]
        cleaner = broker.get('cleaner')
        ctx = _get_context(self.context, broker)
        root = ctx.root
        if isinstance(source, ContentProvider):
            source = source.content
        if not isinstance(source, (list, set)):
            source = [source]
        for e in source:
            pattern = ctx.locate_path(self.path % e)
            for p in glob(os.path.join(root, pattern.lstrip('/'))):
                if self.ignore_func(p) or os.path.isdir(p):
                    continue
                try:
                    result.append(
                        self.kind(
                            p[len(root) :],
                            root=root,
                            save_as=self.save_as,
                            ds=self,
                            ctx=ctx,
                            cleaner=cleaner,
                        )
                    )
                except NoFilterException as nfe:
                    raise nfe
                except Exception:
                    log.debug(traceback.format_exc())
        if result:
            return result
        raise ContentException("No results found for [%s]" % self.path)


class container_execute(foreach_execute):
    """
    Execute a command for each element in provider in container. Provider is
    the output of a different datasource that returns a list of tuples. In each
    tuple, the container engine provider ("podman" or "docker") and the
    container_id are two required elements, the rest elements if there are, are
    the arguments being passed to the command.

    Args:
        provider (list): a list of tuples, in each tuple, the container engine
            provider ("podman" or "docker") and the container_id are two
            required elements, the rest elements if there are, are the
            arguments being passed to the `cmd`.
        cmd (str): a command with substitution parameters. Breaking
            apart a command string that might contain multiple commands
            separated by a pipe, getting them ready for subproc operations.
            IE. A command with filters applied
        context (ExecutionContext): the context under which the datasource
            should run.
        split (bool): whether the output of the command should be split into a
            list of lines
        keep_rc (bool): whether to return the error code returned by the
            process executing the command. If False, any return code other than
            zero with raise a CalledProcessError. If True, the return code and
            output are always returned.
        timeout (int): Number of seconds to wait for the command to complete.
            If the timeout is reached before the command returns, a
            CalledProcessError is raised. If None, timeout is infinite.
        inherit_env (list): The list of environment variables to inherit from the
            calling process when the command is invoked.

    Returns:
        function: A datasource that returns a list of outputs for each command
            created by substituting each element of provider into the cmd template.
    """

    def __call__(self, broker):
        result = []
        source = broker[self.provider]
        cleaner = broker.get('cleaner')
        ctx = broker[self.context]
        if isinstance(source, ContentProvider):
            source = source.content
        if not isinstance(source, (list, set)):
            source = [source]
        for e in source:
            try:
                # e       = (image, <podman|docker>, container_id, <args>)
                image, engine, cid, args = e[0], e[1], e[2], e[3:]
                # handle command with args
                cmd = self.cmd % args if args else self.cmd
                # the_cmd = <podman|docker> exec container_id cmd
                the_cmd = "/usr/bin/%s exec %s %s" % (engine, cid, cmd)
                ccp = ContainerCommandProvider(
                    the_cmd,
                    ctx,
                    image=image,
                    args=e,
                    split=self.split,
                    keep_rc=self.keep_rc,
                    ds=self,
                    timeout=self.timeout,
                    inherit_env=self.inherit_env,
                    override_env=self.override_env,
                    signum=self.signum,
                    cleaner=cleaner,
                )
                result.append(ccp)
            except NoFilterException as nfe:
                raise nfe
            except Exception:
                log.debug(traceback.format_exc())
        if result:
            return result
        raise ContentException("No results found for [%s]" % self.cmd)


class container_collect(foreach_execute):
    """
    Collects the files at the resulting path in running containers.

    Args:
        provider (list): a list of tuples.
        path (str): the file path template with substitution parameters.  The
            path can also be passed via the provider when it's variable per
            cases, in that case, the `path` should be None.
        context (ExecutionContext): the context under which the datasource
            should run.
        keep_rc (bool): whether to return the error code returned by the
            process executing the command. If False, any return code other than
            zero with raise a CalledProcessError. If True, the return code and
            output are always returned.
        timeout (int): Number of seconds to wait for the command to complete.
            If the timeout is reached before the command returns, a
            CalledProcessError is raised. If None, timeout is infinite.

    Returns:
        function: A datasource that returns a list of file contents created by
            substituting each element of provider into the path template.
    """

    def __init__(
        self,
        provider,
        path=None,
        context=HostContext,
        deps=None,
        split=True,
        keep_rc=False,
        timeout=None,
        inherit_env=None,
        override_env=None,
        signum=None,
        **kwargs
    ):
        super(container_collect, self).__init__(
            provider,
            path,
            context,
            deps,
            split,
            keep_rc,
            timeout,
            inherit_env,
            override_env,
            signum,
            **kwargs
        )

    def __call__(self, broker):
        result = []
        source = broker[self.provider]
        cleaner = broker.get('cleaner')
        ctx = broker[self.context]
        if isinstance(source, ContentProvider):
            source = source.content
        if not isinstance(source, (list, set)):
            source = [source]
        for e in source:
            try:
                # e       = (image, <podman|docker>, container_id, <path>)
                image, e = e[0], e[1:]
                if self.cmd is None or self.cmd == '%s':
                    # path is provided by `provider`
                    e, path = e[:-1], e[-1]
                else:
                    # path is provided by self.cmd
                    e, path = e, self.cmd
                # e       = (<podman|docker>, container_id)
                # the_cmd = <podman|docker> exec container_id cat path
                the_cmd = ("/usr/bin/%s exec %s cat " % e) + path
                cfp = ContainerFileProvider(
                    the_cmd,
                    ctx,
                    image=image,
                    args=None,
                    split=self.split,
                    keep_rc=self.keep_rc,
                    ds=self,
                    timeout=self.timeout,
                    inherit_env=self.inherit_env,
                    override_env=self.override_env,
                    signum=self.signum,
                    cleaner=cleaner,
                )
                result.append(cfp)
            except NoFilterException as nfe:
                raise nfe
            except Exception:
                log.debug(traceback.format_exc())
        if result:
            return result
        raise ContentException("No results found for [%s]" % self.cmd)


class first_of(object):
    """Given a list of dependencies, returns the first of the list
    that exists in the broker. At least one must be present, or this
    component won't fire.
    """

    def __init__(self, deps):
        self.deps = deps
        self.raw = getattr(deps[0], 'raw', None)
        self.__name__ = self.__class__.__name__
        datasource(deps)(self)

    def __call__(self, broker):
        for c in self.deps:
            if c in broker:
                return broker[c]


class find(object):
    """
    Helper class for extracting specific lines from a datasource for direct
    consumption by a rule.

    .. code:: python

        service_starts = find(Specs.audit_log, "SERVICE_START")

        @rule(service_starts)
        def report(starts):
            return make_info("SERVICE_STARTS", num_starts=len(starts))

    Args:
        spec (datasource): some datasource, ideally filterable.
        pattern (string / list): a string or list of strings to match (no
            patterns supported)

    Returns:
        A dict where each key is a command, path, or spec name, and each value
        is a non-empty list of matching lines. Only paths with matching lines
        are included.

    Raises:
        SkipComponent: if no paths have matching lines.
    """

    def __init__(self, spec, pattern):
        if getattr(spec, "raw", False):
            name = dr.get_name(spec)
            raise ValueError("{}: Cannot filter raw files.".format(name))

        self.spec = spec
        self.pattern = pattern if isinstance(pattern, list) else [pattern]
        self.__name__ = self.__class__.__name__
        self.__module__ = self.__class__.__module__

        if getattr(spec, "filterable", False):
            filters._add_filter(spec, pattern)

        component(spec)(self)

    def __call__(self, ds):
        # /usr/bin/grep level filtering is applied behind .content or
        # .stream(), but we still need to ensure we get only what *this* find
        # instance wants. This can be inefficient on files where many lines
        # match.
        results = {}
        ds = ds if isinstance(ds, list) else [ds]
        for d in ds:
            if d.relative_path:
                origin = os.path.join("/", d.relative_path.lstrip("/"))
            elif d.cmd:
                origin = d.cmd
            else:
                origin = dr.get_name(self.spec)
            stream = d.content if d.loaded else d.stream()
            lines = []
            for line in stream:
                if any(p in line for p in self.pattern):
                    lines.append(line)
            if lines:
                results[origin] = lines
        if not results:
            raise SkipComponent()
        return dict(results)


@serializer(CommandOutputProvider)
def serialize_command_output(obj, root):
    rel = os.path.join("insights_commands", obj.relative_path)
    if obj.save_as:
        rel = os.path.join("insights_commands", obj.save_as)
        if obj.save_as.endswith("/"):
            rel = os.path.join(rel, os.path.basename(obj.relative_path))
    dst = os.path.join(root, rel)
    rc = obj.write(dst)
    return {
        "rc": rc,
        "cmd": obj.cmd,
        "args": obj.args,
        "save_as": bool(obj.save_as),
        "relative_path": rel,
    }


@deserializer(CommandOutputProvider)
def deserialize_command_output(_type, data, root, ctx, ds):
    rel = data["relative_path"]

    res = SerializedOutputProvider(rel, root=root, ctx=ctx, ds=ds)

    res.rc = data["rc"]
    res.cmd = data["cmd"]
    res.args = data["args"]
    return res


@serializer(TextFileProvider)
def serialize_text_file_provider(obj, root):
    rel = obj.relative_path
    if obj.save_as:
        rel = obj.save_as
        if obj.save_as.endswith("/"):
            rel = os.path.join(rel, os.path.basename(obj.relative_path))
    dst = os.path.join(root, rel)
    rc = obj.write(dst)
    return {
        "save_as": bool(obj.save_as),
        "relative_path": rel,
        "rc": rc,
    }


@deserializer(TextFileProvider)
def deserialize_text_provider(_type, data, root, ctx, ds):
    rel = data["relative_path"]
    res = SerializedOutputProvider(rel, root=root, ctx=ctx, ds=ds)
    res.rc = data["rc"]
    return res


@serializer(RawFileProvider)
def serialize_raw_file_provider(obj, root):
    rel = obj.relative_path
    if obj.save_as:
        rel = obj.save_as
        if obj.save_as.endswith("/"):
            rel = os.path.join(rel, os.path.basename(obj.relative_path))
    dst = os.path.join(root, rel)
    rc = obj.write(dst)
    return {
        "save_as": bool(obj.save_as),
        "relative_path": rel,
        "rc": rc,
    }


@deserializer(RawFileProvider)
def deserialize_raw_file_provider(_type, data, root, ctx, ds):
    rel = data["relative_path"]
    res = SerializedRawOutputProvider(rel, root=root, ctx=ctx, ds=ds)
    res.rc = data["rc"]
    return res


@serializer(DatasourceProvider)
def serialize_datasource_provider(obj, root):
    rel = obj.relative_path
    if obj.save_as:
        rel = obj.save_as
        if obj.save_as.endswith("/"):
            rel = os.path.join(rel, os.path.basename(obj.relative_path))
    dst = os.path.join(root, rel)
    obj.write(dst)
    return {"relative_path": rel, "save_as": obj.save_as}


@deserializer(DatasourceProvider)
def deserialize_datasource_provider(_type, data, root, ctx, ds):
    res = SerializedOutputProvider(data["relative_path"], root=root, ctx=ctx, ds=ds)
    return res


@serializer(ContainerFileProvider)
def serialize_container_file_output(obj, root):
    rel = os.path.join("insights_containers", obj.relative_path)
    if obj.save_as:
        rel = os.path.join("insights_containers", obj.save_as)
        if obj.save_as.endswith("/"):
            rel = os.path.join(rel, os.path.basename(obj.relative_path))
    dst = os.path.join(root, rel)
    rc = obj.write(dst)
    return {
        "save_as": bool(obj.save_as),
        "relative_path": rel,
        "rc": rc,
        "image": obj.image,
        "engine": obj.engine,
        "container_id": obj.container_id,
    }


@deserializer(ContainerFileProvider)
def deserialize_container_file(_type, data, root, ctx, ds):
    rel = data["relative_path"]
    res = SerializedOutputProvider(rel, root=root, ctx=ctx, ds=ds)
    res.rc = data["rc"]
    res.image = data["image"]
    res.engine = data["engine"]
    res.container_id = data["container_id"]
    return res


@serializer(ContainerCommandProvider)
def serialize_container_command(obj, root):
    rel = os.path.join("insights_containers", obj.relative_path)
    if obj.save_as:
        rel = os.path.join("insights_containers", obj.save_as)
        if obj.save_as.endswith("/"):
            rel = os.path.join(rel, os.path.basename(obj.relative_path))
    dst = os.path.join(root, rel)
    rc = obj.write(dst)
    return {
        "rc": rc,
        "cmd": obj.cmd,
        "args": obj.args,
        "save_as": bool(obj.save_as),
        "relative_path": rel,
        "image": obj.image,
        "engine": obj.engine,
        "container_id": obj.container_id,
    }


@deserializer(ContainerCommandProvider)
def deserialize_container_command(_type, data, root, ctx, ds):
    rel = data["relative_path"]
    res = SerializedOutputProvider(rel, root=root, ctx=ctx, ds=ds)
    res.rc = data["rc"]
    res.cmd = data["cmd"]
    res.args = data["args"]
    res.image = data["image"]
    res.engine = data["engine"]
    res.container_id = data["container_id"]
    return res
