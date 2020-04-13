#!/usr/bin/env python
"""dirhash - a python module (and CLI) for hashing of file system directories.

Provides the the following public functions and classes:
- `dirhash`
- `included_paths`
- `Filter`
- `get_match_patterns`
- `Protocol`
"""
from __future__ import print_function, division

import os
import hashlib
import pkg_resources

from functools import partial
from multiprocessing import Pool

from scantree import (
    scantree,
    RecursionFilter,
    CyclicLinkedDir,
)

__all__ = [
    '__version__',
    'algorithms_guaranteed',
    'algorithms_available',
    'dirhash',
    'included_paths',
    'Filter',
    'get_match_patterns',
    'Protocol'
]

__version__ = pkg_resources.require("dirhash")[0].version

algorithms_guaranteed = {'md5', 'sha1', 'sha224', 'sha256', 'sha384', 'sha512'}
algorithms_available = hashlib.algorithms_available


def dirhash(
    directory,
    algorithm,
    filtering=None,
    protocol=None,
    chunk_size=2**20,
    jobs=1
):
    """Computes the hash of a directory based on its structure and content.

    # Arguments
        directory: Union[str, pathlib.Path] - Path to the directory to hash.
        algorithm: str - The name of the hashing algorithm to use. See
            `dirhash.algorithms_available` for the available options.
            It is also possible to provide a callable object that returns an instance
            implementing the `hashlib._hashlib.HASH` interface.
        filtering: Optional[Union[dirhash.Filter, Dict[str, str]]] - An instance of
            dirhash.Filter or a dictionary of keyword arguments for the same.
            Determines what paths within the `directory` to include when computing
            the hash value. Default `None`, which means that all files and
            directories are included  *except for empty directories*.
                The `dirhash.Filter` supports glob/wildcard (".gitignore style") path
            matching by the `match` argument. Paths *relative to the root `directory`
            (i.e. excluding the name of the root directory itself) are matched
            against the provided patterns. For example, to include all files,
            except for hidden ones use: `filtering={'match': ['*', '!.*']}`
            (or the equivalent `filtering=Filter(match=['*', '!.*'])`). For
            inspection and verification, you can pass the `filtering` argument to
            `dirhash.included_paths` to get a list of all paths that would be
            included when computing the hash value.
                For further options and details, see `dirhash.Filter`.
        protocol: Optional[Union[dirhash.Protocol, Dict[str, str]]] - An instance of
            dirhash.Protocol or a dictionary of keyword arguments for the same.
            Determines (mainly) what properties of files and directories to consider
            when computing the hash value. Default `None`, which means that both the
            name and content (actual data) of files and directories will be included.
                To only hash the "file structure", as in the name of files and
            directories and their location relative to the root `directory`, use:
            `protocol={'entry_properties': ['name']}`. Contrary, to only hash the
            data and ignoring the name of directories and files use
            `protocol={'entry_properties': ['data']}`. NOTE that the tree structure
            in which files are organized under the root `directory` still influences
            the computed hash with this option. As longs as all files have the same
            content and are organised the same way in relation to all other files in
            the Directed Acyclic Graph representing the file tree, the hash will
            remain the same (but the "name of nodes" does not matter). This option
            can e.g. be used to verify that that data is unchanged after renaming
            files (change extensions etc.).
                For further options and details, see `dirhash.Protocol`.
        chunk_size: int - The number of bytes to read in one go from files while
            being hashed. A too small size will slow down the processing and a larger
            size consumes more working memory. Default 2**20 byte = 1 MiB.
        jobs: int - The number of processes to use when computing the hash.
            Default `1`, which means that a single (the main) process is used. NOTE
            that using multiprocessing can significantly speed-up execution, see
            `https://github.com/andhus/dirhash/tree/master/benchmark` for further
            details.

    # Returns
        str - The hash/checksum as a string of the hexadecimal digits (the result of
        `hexdigest` method of the hashlib._hashlib.HASH object corresponding to the
        provided `algorithm`).

    # Raises
        TypeError/ValueError: For incorrectly provided arguments.
        SymlinkRecursionError: In case the `directory` contains symbolic links that
            lead to (infinite) recursion and `protocol=None` (default) or
            `protocol={'on_cyclic_links': 'raise'}`.
                To be able to hash  directories with cyclic links use
            `protocol={'on_cyclic_links': 'hash reference'}`.

    # References
        See https://github.com/andhus/dirhash/DIRHASH_STANDARD.md for a formal
        description of how the returned hash value is computed.
    """

    filter_ = _get_instance('filtering', filtering, Filter)
    protocol = _get_instance('protocol', protocol, Protocol)
    hasher_factory = _get_hasher_factory(algorithm)
    allow_cyclic_links = protocol.on_cyclic_link != protocol.OnCyclicLink.RAISE

    def dir_apply(dir_node):
        if not filter_.empty_dirs:
            if dir_node.path.relative == '' and dir_node.empty:
                # only check if root node is empty (other empty dirs are filter
                # before `dir_apply` with `filter_.empty_dirs=False`)
                raise ValueError('{}: Nothing to hash'.format(directory))
        descriptor = protocol.get_descriptor(dir_node)
        _dirhash = hasher_factory(descriptor.encode('utf-8')).hexdigest()

        return dir_node.path, _dirhash

    if jobs == 1:
        cache = {}

        def file_apply(path):
            return path, _get_filehash(
                path.real,
                hasher_factory,
                chunk_size=chunk_size,
                cache=cache
            )

        _, dirhash_ = scantree(
            directory,
            recursion_filter=filter_,
            file_apply=file_apply,
            dir_apply=dir_apply,
            follow_links=True,
            allow_cyclic_links=allow_cyclic_links,
            cache_file_apply=False,
            include_empty=filter_.empty_dirs,
            jobs=1
        )
    else:  # multiprocessing
        real_paths = set()

        def extract_real_paths(path):
            real_paths.add(path.real)
            return path

        root_node = scantree(
            directory,
            recursion_filter=filter_,
            file_apply=extract_real_paths,
            follow_links=True,
            allow_cyclic_links=allow_cyclic_links,
            cache_file_apply=False,
            include_empty=filter_.empty_dirs,
            jobs=1
        )
        real_paths = list(real_paths)
        # hash files in parallel
        file_hashes = _parmap(
            partial(
                _get_filehash,
                hasher_factory=hasher_factory,
                chunk_size=chunk_size
            ),
            real_paths,
            jobs=jobs
        )
        # prepare the mapping with precomputed file hashes
        real_path_to_hash = dict(zip(real_paths, file_hashes))

        def file_apply(path):
            return path, real_path_to_hash[path.real]

        _, dirhash_ = root_node.apply(file_apply=file_apply, dir_apply=dir_apply)

    return dirhash_


def included_paths(
    directory,
    filtering=None,
    protocol=None
):
    """Inspect what paths are included for the corresponding arguments to the
    `dirhash.dirhash` function.

    # Arguments:
        This function accepts the following subset of the function `dirhash.dirhash`
        arguments: `directory`, `filtering` and `protocol`, with the same meaning.
        See docs of `dirhash.dirhash` for further details.

    # Returns
        List[str] - A sorted list of the paths that would be included when computing
        the hash of `directory` using `dirhash.dirhash` and the same arguments.
    """
    protocol = _get_instance('protocol', protocol, Protocol)
    filter_ = _get_instance('filtering', filtering, Filter)
    allow_cyclic_links = protocol.on_cyclic_link != protocol.OnCyclicLink.RAISE

    leafpaths = scantree(
        directory,
        recursion_filter=filter_,
        follow_links=True,
        allow_cyclic_links=allow_cyclic_links,
        include_empty=filter_.empty_dirs
    ).leafpaths()

    return [
        path.relative if path.is_file() else os.path.join(path.relative, '.')
        for path in leafpaths
    ]


class Filter(RecursionFilter):

    """Specification of what files and directories to include for the `dirhash`
    computation.

    # Arguments
        match: Optional[List[str]] - A list of glob/wildcard (".gitignore style")
            match patterns for selection of which files and directories to include.
            Paths *relative to the root `directory` (i.e. excluding the name of the
            root directory itself) are matched against the provided patterns. For
            example, to include all files, except for hidden ones use:
            `match=['*', '!.*']` Default `None` which is equivalent to `['*']`,
            i.e. everything is included.
        linked_dirs: bool - If `True` (default), follow symbolic links to other
            *directories* and include these and their content in the hash
            computation.
        linked_files: bool - If `True` (default), include symbolic linked files in
            the hash computation.
        empty_dirs: bool - If `True`, include empty directories when computing the
            hash. A directory is considered empty if it does not contain any files
            that *matches provided matching criteria*. Default `False`, i.e. empty
            directories are ignored (as is done in git version control).

    NOTE: To inspection/verify which paths are included, pass an instance of this
    class to `dirhash.included_paths`.
    """
    def __init__(
        self,
        match=None,
        linked_dirs=True,
        linked_files=True,
        empty_dirs=False
    ):
        super(Filter, self).__init__(
            linked_dirs=linked_dirs,
            linked_files=linked_files,
            match=match
        )
        self.empty_dirs = empty_dirs


def get_match_patterns(
    match=None,
    ignore=None,
    ignore_extensions=None,
    ignore_hidden=False,
):
    """Helper to compose a list of list of glob/wildcard (".gitignore style") match
    patterns based on options dedicated for a few standard use-cases.

    # Arguments
        match: Optional[List[str]] - A list of match-patterns for files to *include*.
            Default `None` which is equivalent to `['*']`, i.e. everything is
            included (unless excluded by arguments below).
        ignore: Optional[List[str]] -  A list of match-patterns for files to
            *ignore*. Default `None` (no ignore patterns).
        ignore_extensions: Optional[List[str]] -  A list of file extensions to
            ignore. Short for `ignore=['*.<my extension>', ...]` Default `None` (no
            extensions ignored).
        ignore_hidden: bool - If `True` ignore hidden files and directories. Short
            for `ignore=['.*', '.*/']` Default `False`.
    """
    match = ['*'] if match is None else list(match)
    ignore = [] if ignore is None else list(ignore)
    ignore_extensions = [] if ignore_extensions is None else list(ignore_extensions)

    if ignore_hidden:
        ignore.extend(['.*', '.*/'])

    for ext in ignore_extensions:
        if not ext.startswith('.'):
            ext = '.' + ext
        ext = '*' + ext
        ignore.append(ext)

    match_spec = match + ['!' + ign for ign in ignore]

    def deduplicate(items):
        items_set = set([])
        dd_items = []
        for item in items:
            if item not in items_set:
                dd_items.append(item)
                items_set.add(item)

        return dd_items

    return deduplicate(match_spec)


class Protocol(object):
    """Specifications of which file and directory properties to consider when
        computing the `dirhash` value.

    # Arguments
        entry_properties: List[str] - Must be one of the following combinations:
            - ["name", "data"] (Default) - The name as well as data is included.
            - ["data"] - Compute the hash only based on the data of files -
                *not* their names or the names of their parent directories. NOTE that
                the tree structure in which files are organized under the `directory`
                root still influences the computed hash. As longs as all files have
                the same content and are organised the same way in relation to all
                other files in the Directed Acyclic Graph representing the file-tree,
                the hash will remain the same (but the "name of nodes" does not
                matter). This option can e.g. be used to verify that that data is
                unchanged after renaming files (change extensions etc.).
            - ["name"] - Compute the hash only based on the name and location of
                files in the file tree under the `directory` root. This option can
                e.g. be used to check if any files have been added/moved/removed,
                ignoring the content of each file.
        on_cyclic_link: str - One of:
            - "raise" - A `SymlinkRecursionError` is raised on presence of cyclic
                symbolic links.
            - "hash_reference"
    """
    class OnCyclicLink(object):
        RAISE = 'raise'
        HASH_REFERENCE = 'hash_reference'
        options = {RAISE, HASH_REFERENCE}

    class EntryProperties(object):
        NAME = 'name'
        DATA = 'data'
        IS_LINK = 'is_link'
        options = {NAME, DATA, IS_LINK}
        _DIRHASH = 'dirhash'

    _entry_property_separator = '\000'
    _entry_descriptor_separator = '\000\000'

    def __init__(
        self,
        entry_properties=('name', 'data'),
        on_cyclic_link='raise'
    ):
        entry_properties = set(entry_properties)
        if not entry_properties.issubset(self.EntryProperties.options):
            raise ValueError(
                'entry properties {} not supported'.format(
                    entry_properties - self.EntryProperties.options)
            )
        if not (
            self.EntryProperties.NAME in entry_properties or
            self.EntryProperties.DATA in entry_properties
        ):
            raise ValueError(
                'at least one of entry properties `name` and `data` must be used'
            )
        self.entry_properties = entry_properties
        self._include_name = self.EntryProperties.NAME in entry_properties
        self._include_data = self.EntryProperties.DATA in entry_properties
        self._include_is_link = self.EntryProperties.IS_LINK in entry_properties

        if on_cyclic_link not in self.OnCyclicLink.options:
            raise ValueError(
                '{}: not a valid on_cyclic_link option'.format(on_cyclic_link)
            )
        self.on_cyclic_link = on_cyclic_link

    def get_descriptor(self, dir_node):
        if isinstance(dir_node, CyclicLinkedDir):
            return self._get_cyclic_linked_dir_descriptor(dir_node)

        entries = dir_node.directories + dir_node.files
        entry_descriptors = [
            self._get_entry_descriptor(
                self._get_entry_properties(path, entry_hash)
            ) for path, entry_hash in entries
        ]
        return self._entry_descriptor_separator.join(sorted(entry_descriptors))

    @classmethod
    def _get_entry_descriptor(cls, entry_properties):
        entry_strings = [
            '{}:{}'.format(name, value)
            for name, value in entry_properties
        ]
        return cls._entry_property_separator.join(sorted(entry_strings))

    def _get_entry_properties(self, path, entry_hash):
        properties = []
        if path.is_dir():
            properties.append((self.EntryProperties._DIRHASH, entry_hash))
        elif self._include_data:  # path is file
            properties.append((self.EntryProperties.DATA, entry_hash))

        if self._include_name:
            properties.append((self.EntryProperties.NAME, path.name))
        if self._include_is_link:
            properties.append((self.EntryProperties.IS_LINK, path.is_symlink))

        return properties

    def _get_cyclic_linked_dir_descriptor(self, dir_node):
        relpath = dir_node.path.relative
        target_relpath = dir_node.target_path.relative
        path_to_target = os.path.relpath(
            # the extra '.' is needed if link back to root, because
            # an empty path ('') is not supported by os.path.relpath
            os.path.join('.', target_relpath),
            os.path.join('.', relpath)
        )
        # TODO normalize posix!
        return path_to_target


def _get_hasher_factory(algorithm):
    """Returns a "factory" of hasher instances corresponding to the given algorithm
    name. Bypasses input argument `algorithm` if it is already a hasher factory
    (verified by attempting calls to required methods).
    """
    if algorithm in algorithms_guaranteed:
        return getattr(hashlib, algorithm)

    if algorithm in algorithms_available:
        return partial(hashlib.new, algorithm)

    try:  # bypass algorithm if already a hasher factory
        hasher = algorithm(b'')
        hasher.update(b'')
        hasher.hexdigest()
        return algorithm
    except:
        pass

    raise ValueError(
        '`algorithm` must be one of: {}`'.format(algorithms_available))


def _parmap(func, iterable, jobs=1):
    if jobs == 1:
        return [func(element) for element in iterable]

    pool = Pool(jobs)
    try:
        results = pool.map(func, iterable)
    finally:
        pool.close()

    return results


def _get_instance(argname, instance_or_kwargs, cls):
    if instance_or_kwargs is None:
        return cls()
    if isinstance(instance_or_kwargs, dict):
        return cls(**instance_or_kwargs)
    if isinstance(instance_or_kwargs, cls):
        return instance_or_kwargs
    raise TypeError(
        'argument {argname} must be an instance of, or kwargs for, '
        '{cls}'.format(argname=argname, cls=cls)
    )


def _get_filehash(filepath, hasher_factory, chunk_size, cache=None):
    """Compute the hash for given filepath.

    # Arguments
        filepath (str): Path to the file to hash.
        hasher_factory (f: f() -> hashlib._hashlib.HASH): Callable that returns an
            instance of the `hashlib._hashlib.HASH` interface.
        chunk_size (int): The number of bytes to read in one go from files while
            being hashed.
        cache ({str: str} | None): A mapping from `filepath` to hash (return value
            of this function). If not None, a lookup will be attempted before hashing
            the file and the result will be added after completion.

    # Returns
        The hash/checksum as a string the of hexadecimal digits.

    # Side-effects
        The `cache` is updated if not None.
    """
    if cache is not None:
        filehash = cache.get(filepath, None)
        if filehash is None:
            filehash = _get_filehash(filepath, hasher_factory, chunk_size)
            cache[filepath] = filehash
        return filehash

    hasher = hasher_factory()
    with open(filepath, 'rb') as f:
        for chunk in iter(lambda: f.read(chunk_size), b''):
            hasher.update(chunk)

    return hasher.hexdigest()

