"""Store and retrieve information about uploaded data files from the database."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional, List

import sqlalchemy.engine

from robot_server.persistence.database import sqlite_rowid
from robot_server.persistence.tables import data_files_table


@dataclass(frozen=True)
class DataFileInfo:
    """Metadata info of a saved data file."""

    id: str
    name: str
    file_hash: str
    created_at: datetime


class DataFilesStore:
    """Store and retrieve info about uploaded data files."""

    def __init__(
        self,
        sql_engine: sqlalchemy.engine.Engine,
    ) -> None:
        """Create a new DataFilesStore."""
        self._sql_engine = sql_engine

    def get_file_info_by_hash(self, file_hash: str) -> Optional[DataFileInfo]:
        """Get the ID of data file having the provided hash."""
        for file in self._sql_get_all_from_engine():
            if file.file_hash == file_hash:
                return file
        return None

    async def insert(self, file_info: DataFileInfo) -> None:
        """Insert data file info in the database."""
        file_info_dict = {
            "id": file_info.id,
            "name": file_info.name,
            "created_at": file_info.created_at,
            "file_hash": file_info.file_hash,
        }
        statement = sqlalchemy.insert(data_files_table).values(file_info_dict)
        with self._sql_engine.begin() as transaction:
            transaction.execute(statement)

    def _sql_get_all_from_engine(self) -> List[DataFileInfo]:
        statement = sqlalchemy.select(data_files_table).order_by(sqlite_rowid)
        with self._sql_engine.begin() as transaction:
            all_rows = transaction.execute(statement).all()
        return [
            DataFileInfo(
                id=sql_row.id,
                name=sql_row.name,
                created_at=sql_row.created_at,
                file_hash=sql_row.file_hash,
            )
            for sql_row in all_rows
        ]
