-- Deterministic empty SQLite input for fresh-install migration tests.
-- This source file is never opened as a database; tests materialize it under tmp_path.
PRAGMA page_size=4096;
PRAGMA encoding='UTF-8';
PRAGMA foreign_keys=ON;
