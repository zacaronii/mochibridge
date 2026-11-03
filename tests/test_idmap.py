import os
import sqlite3

import pytest

from kbridge import idmap
from kbridge.db import DB

ANIBRIDGE = os.environ.get("KB_ANIBRIDGE_DB", "")


def make_anibridge(path):
    c = sqlite3.connect(path)
    c.executescript("""
    CREATE TABLE animap_entry (id INTEGER PRIMARY KEY, provider TEXT, entry_id TEXT, entry_scope TEXT);
    CREATE TABLE animap_mapping (id INTEGER PRIMARY KEY, source_entry_id INT, destination_entry_id INT,
        source_range TEXT, destination_range TEXT);
    INSERT INTO animap_entry VALUES (1,'anidb','90017','R'),(2,'anilist','900017',NULL),
        (3,'anidb','90017','S'),(4,'anilist','999',NULL),(5,'tvdb_show','900069','s1'),
        (6,'anidb','500','R'),(7,'anilist','501',NULL),(8,'anilist','502',NULL);
    INSERT INTO animap_mapping VALUES (1,1,2,'1-11','1-11'),(2,3,4,'1','1'),(3,1,5,'1-11','1-11'),
        (4,6,8,'13-24','1-12'),(5,6,7,'1-12','1-12');
    """)
    c.commit()
    c.close()


def test_refresh_and_lookup(tmp_path):
    src = tmp_path / "anibridge.db"
    make_anibridge(src)
    db = DB(":memory:")
    assert idmap.refresh(db, str(src)) == 5
    assert idmap.refresh(db, str(src)) is None                       # fresh enough, skipped
    assert idmap.lookup(db, "anidb", 90017) == {"anilist": "900017", "tvdb_show": "900069:s1"}
    assert idmap.lookup(db, "anidb", "90017", scope="S") == {"anilist": "999"}
    assert idmap.anilist_id(db, "anidb", 500) == "501"               # earliest range wins over 2nd cour
    assert idmap.anilist_id(db, "anidb", 1) is None


def test_missing_db_keeps_previous(tmp_path):
    src = tmp_path / "anibridge.db"
    make_anibridge(src)
    db = DB(":memory:")
    idmap.refresh(db, str(src))
    assert idmap.refresh(db, str(tmp_path / "gone.db"), force=True) is None
    assert idmap.anilist_id(db, "anidb", 90017) == "900017"


@pytest.mark.skipif(not os.path.exists(ANIBRIDGE), reason="anibridge DB not mounted")
def test_real_anibridge_db_readonly():
    before = {p: os.stat(p).st_mtime_ns for p in (ANIBRIDGE, ANIBRIDGE + "-wal") if os.path.exists(p)}
    db = DB(":memory:")
    assert idmap.refresh(db, ANIBRIDGE, force=True) > 100_000
    assert idmap.anilist_id(db, "anidb", 23) == "1"                 # a well-known public mapping (AniDB 23 = AniList 1)
    after = {p: os.stat(p).st_mtime_ns for p in before}
    assert before == after
