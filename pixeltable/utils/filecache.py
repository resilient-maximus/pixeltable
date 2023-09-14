from __future__ import annotations
from typing import Optional, List, Tuple, Dict
from collections import OrderedDict, defaultdict, namedtuple
import os
import re
import glob
from dataclasses import dataclass
from pathlib import Path
from time import time
import logging
from uuid import UUID

from pixeltable.env import Env


_logger = logging.getLogger('pixeltable')

@dataclass(eq=True, frozen=True)
class CellId:
    tbl_id: UUID  # catalog.TableVersion.id
    col_id: int  # catalog.Column.id

    # primary key values; the length depends on the type of store table; needs to be a Tuple in order to be hashable
    pk: Tuple[int]


class CacheEntry:
    def __init__(self, cell_id: CellId, size: int, last_accessed_ts: int):
        self.cell_id = cell_id
        self.size = size
        self.last_accessed_ts = last_accessed_ts

    def filename(self) -> str:
        return f'{self.cell_id.tbl_id.hex}_{self.cell_id.col_id}' + ''.join([f'_{v}' for v in self.cell_id.pk])

    def path(self) -> Path:
        return Env.get().filecache_dir / self.filename()

    @classmethod
    def from_file(cls, path: Path) -> CacheEntry:
        components = path.name.split('_')
        assert len(components) >= 3
        tbl_id = UUID(components[0])
        col_id = int(components[1])
        pk = [int(c) for c in components[2:]]
        file_info = os.stat(str(path))
        return cls(CellId(tbl_id, col_id, tuple(pk)), file_info.st_size, file_info.st_mtime)


class FileCache:
    """
    A local cache of external (eg, S3) file references in cells of a stored table (ie, table or view).

    TODO:
    - enforce a maximum capacity with LRU eviction
    - implement MRU eviction for queries that exceed the capacity
    """
    _instance: Optional[FileCache] = None
    ColumnStats = namedtuple('FileCacheColumnStats', ['tbl_id', 'col_id', 'num_files', 'total_size'])
    CacheStats = namedtuple('FileCacheStats', ['num_requests', 'num_hits', 'num_evictions', 'util'])

    @classmethod
    def get(cls) -> FileCache:
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def __init__(self):
        paths = glob.glob(str(Env.get().filecache_dir / '*'))
        self.cache: OrderedDict[CellId, CacheEntry] = OrderedDict()  # ordered by entry.last_accessed_ts
        self.total_size = 0
        #self.capacity = Env.get().max_filecache_size
        self.num_requests = 0
        self.num_hits = 0
        self.num_evictions = 0
        entries = [CacheEntry.from_file(Path(path_str)) for path_str in paths]
        # we need to insert entries in order of last_accessed_ts
        entries.sort(key=lambda e: e.last_accessed_ts)
        for entry in entries:
            self.cache[entry.cell_id] = entry
            self.total_size += entry.size

    def avg_file_size(self) -> int:
        if len(self.cache) == 0:
            return 0
        return int(self.total_size / len(self.cache))

    def num_files(self, tbl_id: Optional[UUID] = None) -> int:
        if tbl_id is None:
            return len(self.cache)
        entries = [e for e in self.cache.values() if e.cell_id.tbl_id == tbl_id]
        return len(entries)

    def clear(self, tbl_id: Optional[UUID] = None, capacity: Optional[int] = None) -> None:
        """
        For testing purposes: allow resetting capacity and stats.
        """
        self.num_requests, self.num_hits, self.num_evictions = 0, 0, 0
        entries = list(self.cache.values())  # list(): avoid dealing with values() return type
        if tbl_id is not None:
            entries = [e for e in entries if e.cell_id.tbl_id == tbl_id]
            _logger.debug(f'clearing {len(entries)} entries from file cache for table {tbl_id}')
        else:
            _logger.debug(f'clearing {len(entries)} entries from file cache')
        for entry in entries:
            del self.cache[entry.cell_id]
            self.total_size -= entry.size
            os.remove(entry.path())
        # if capacity is not None:
        #     self.capacity = capacity
        # else:
        #     # need to reset to default
        #     self.capacity = Env.get().max_filecache_size
        # _logger.debug(f'setting file cache capacity to {self.capacity}')

    def lookup(self, tbl_id: UUID, col_id: int, pk: List[int]) -> Optional[Path]:
        self.num_requests += 1
        cell_id = CellId(tbl_id, col_id, tuple(pk))
        entry = self.cache.get(cell_id, None)
        if entry is None:
            _logger.debug(f'file cache miss for {cell_id}')
            return None
        # update mtime and cache
        path = entry.path()
        path.touch(exist_ok=True)
        file_info = os.stat(str(path))
        entry.last_accessed_ts = file_info.st_mtime
        self.cache.move_to_end(cell_id, last=True)
        self.num_hits += 1
        _logger.debug(f'file cache hit for {cell_id}')
        return path

    # def can_admit(self, query_ts: int) -> bool:
    #     if self.total_size + self.avg_file_size <= self.capacity:
    #         return True
    #     assert len(self.cache) > 0
    #     # check whether we can evict the current lru entry
    #     lru_entry = next(iter(self.cache.values()))
    #     if lru_entry.last_accessed_ts >= query_ts:
    #         # the current query brought this entry in: we're not going to evict it
    #         return False
    #     return True

    def add(self, tbl_id: UUID, col_id: int, pk: List[int], path: Path) -> Path:
        """Adds file at 'path' to cache and returns its new path.
        'path' will not be accessible after this call.
        """
        file_info = os.stat(str(path))
        _ = time()
        #if self.total_size + file_info.st_size > self.capacity:
        if False:
            if len(self.cache) == 0:
                # nothing to evict
                return
            # evict entries until we're below the limit or until we run into entries the current query brought in
            while True:
                lru_entry = next(iter(self.cache.values()))
                if lru_entry.last_accessed_ts >= query_ts:
                    # the current query brought this entry in: switch to MRU and ignore this put()
                    _logger.debug('file cache switched to MRU')
                    return
                self.cache.popitem(last=False)
                self.total_size -= lru_entry.size
                self.num_evictions += 1
                os.remove(str(lru_entry.path()))
                _logger.debug(f'evicted entry for cell {lru_entry.cell_id} from file cache')
                if self.total_size + file_info.st_size <= self.capacity:
                    break

        cell_id = CellId(tbl_id, col_id, tuple(pk))
        entry = CacheEntry(cell_id, file_info.st_size, file_info.st_mtime)
        self.cache[entry.cell_id] = entry
        self.total_size += entry.size
        new_path = entry.path()
        os.rename(str(path), str(new_path))
        _logger.debug(f'added entry for cell {cell_id} to file cache')
        return new_path

    def stats(self) -> CacheStats:
        # collect column stats
        d: Dict[Tuple[int, int], List[int]] = defaultdict(lambda: [0, 0])
        for entry in self.cache.values():
            t = d[(entry.cell_id.tbl_id, entry.cell_id.col_id)]
            t[0] += 1
            t[1] += entry.size
        col_stats = [
            self.ColumnStats(tbl_id, col_id, num_files, size) for (tbl_id, col_id), (num_files, size) in d.items()
        ]
        col_stats.sort(key=lambda e: e[3], reverse=True)
        return self.CacheStats(self.num_requests, self.num_hits, self.num_evictions, col_stats)

    def debug_print(self) -> None:
        for entry in self.cache.values():
            print(f'CacheEntry: tbl_id={entry.cell_id.tbl_id}, col_id={entry.cell_id.col_id}, size={entry.size}')
