from __future__ import annotations

import copy
import logging
from typing import Optional, List, Dict, Type
from uuid import UUID

import sqlalchemy.orm as orm

from .schema_object import SchemaObject
from .dir import Dir
from .path import Path
from pixeltable import exceptions as exc
from pixeltable.env import Env
from pixeltable.metadata import schema

_ID_RE = r'[a-zA-Z]\w*'
_PATH_RE = f'{_ID_RE}(\\.{_ID_RE})*'


_logger = logging.getLogger('pixeltable')

class PathDict:
    """Keep track of all paths in a Db instance"""
    def __init__(self):
        self.dir_contents: Dict[UUID, Dict[str, SchemaObject]] = {}
        self.schema_objs: Dict[UUID, SchemaObject] = {}

        # load dirs
        with orm.Session(Env.get().engine, future=True) as session:
            _ = [dir_record for dir_record in session.query(schema.Dir).all()]
            self.schema_objs = {
                dir_record.id: Dir(dir_record.id, dir_record.parent_id, schema.DirMd(**dir_record.md).name)
                for dir_record in session.query(schema.Dir).all()
            }

        # identify root dir
        root_dirs = [dir for dir in self.schema_objs.values() if dir.dir_id is None]
        assert len(root_dirs) == 1
        self.root_dir = root_dirs[0]

        # build dir_contents
        def record_dir(dir: Dir) -> None:
            if dir.id in self.dir_contents:
                return
            else:
                self.dir_contents[dir.id] = {}
            if dir.dir_id is not None:
                record_dir(self.schema_objs[dir.dir_id])
                self.dir_contents[dir.dir_id][dir.name] = dir

        for dir in self.schema_objs.values():
            record_dir(dir)

    def _resolve_path(self, path: Path) -> SchemaObject:
        if path.is_root:
            return self.root_dir
        dir = self.root_dir
        for i, component in enumerate(path.components):
            if component not in self.dir_contents[dir.id]:
                raise exc.Error(f'No such path: {".".join(path.components[:i+1])}')
            schema_obj = self.dir_contents[dir.id][component]
            if i < len(path.components) - 1:
                if not isinstance(schema_obj, Dir):
                    raise exc.Error(f'Not a directory: {".".join(path.components[:i+1])}')
                dir = schema_obj
        return schema_obj

    def __getitem__(self, path: Path) -> SchemaObject:
        return self._resolve_path(path)

    def get_schema_obj(self, id: UUID) -> Optional[SchemaObject]:
        return self.schema_objs.get(id)

    def add_schema_obj(self, dir_id: UUID, name: str, val: SchemaObject) -> None:
        self.dir_contents[dir_id][name] = val
        self.schema_objs[val.id] = val

    def __setitem__(self, path: Path, val: SchemaObject) -> None:
        parent_dir = self._resolve_path(path.parent)
        assert path.name not in self.dir_contents[parent_dir.id]
        self.schema_objs[val.id] = val
        self.dir_contents[parent_dir.id][path.name] = val
        if isinstance(val, Dir):
            self.dir_contents[val.id] = {}

    def __delitem__(self, path: Path) -> None:
        parent_dir = self._resolve_path(path.parent)
        assert path.name in self.dir_contents[parent_dir.id]
        obj = self.dir_contents[parent_dir.id][path.name]
        del self.dir_contents[parent_dir.id][path.name]
        if isinstance(obj, Dir):
            del self.dir_contents[obj.id]
        del self.schema_objs[obj.id]

    def move(self, from_path: Path, to_path: Path) -> None:
        from_dir = self._resolve_path(from_path.parent)
        assert isinstance(from_dir, Dir)
        assert from_path.name in self.dir_contents[from_dir.id]
        obj = self.dir_contents[from_dir.id][from_path.name]
        del self.dir_contents[from_dir.id][from_path.name]
        to_dir = self._resolve_path(to_path.parent)
        assert to_path.name not in self.dir_contents[to_dir.id]
        self.dir_contents[to_dir.id][to_path.name] = obj

    def check_is_valid(self, path: Path, expected: Optional[Type[SchemaObject]]) -> None:
        """Check that path is valid and that the object at path has the expected type.

        Args:
            path: path to check
            expected: expected type of object at path or None if object should not exist

        Raises:
            Error if path is invalid or object at path has wrong type
        """
        # check for existence
        if expected is not None:
            schema_obj = self._resolve_path(path)
            if not isinstance(schema_obj, expected):
                raise exc.Error(
                    f'{str(path)} needs to be a {expected.display_name()} but is a {type(schema_obj).display_name()}')
        if expected is None:
            parent_obj = self._resolve_path(path.parent)
            if not isinstance(parent_obj, Dir):
                raise exc.Error(
                    f'{str(path.parent)} is a {type(parent_obj).display_name()}, not a {Dir.display_name()}')
            if path.name in self.dir_contents[parent_obj.id]:
                obj = self.dir_contents[parent_obj.id][path.name]
                raise exc.Error(f"{type(obj).display_name()} '{str(path)}' already exists")

    def get_children(self, parent: Path, child_type: Optional[Type[SchemaObject]], recursive: bool) -> List[Path]:
        dir = self._resolve_path(parent)
        if not isinstance(dir, Dir):
            raise exc.Error(f'{str(parent)} is a {type(dir).display_name()}, not a directory')
        matches = [
            obj for obj in self.dir_contents[dir.id].values() if child_type is None or isinstance(obj, child_type)
        ]
        result = [copy.copy(parent).append(obj.name) for obj in matches]
        if recursive:
            for dir in [obj for obj in self.dir_contents[dir.id].values() if isinstance(obj, Dir)]:
                result.extend(self.get_children(copy.copy(parent).append(dir.name), child_type, recursive))
        return result

